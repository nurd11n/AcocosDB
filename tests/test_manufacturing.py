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


def test_audit_fix_flag_is_gone_command_is_read_only(capsys):
    """The base-currency corruption is now impossible three ways over (admin
    freezes the rate, migration 0002 repaired existing rows, a DB constraint
    rejects it even via raw SQL) — so the command that used to repair it is
    read-only again, like audit_stale_totals. Nothing here may write."""
    from django.core.management import call_command

    today = timezone.localdate()
    foreign = Expense.objects.create(
        date=today,
        category=Expense.FABRIC,
        amount=Decimal("10"),
        currency="USD",
        rate_to_kgs=Decimal("1"),
    )
    call_command("audit_expenses")
    out = capsys.readouterr().out
    assert "UNDERSTATED" in out and "only reports" in out
    foreign.refresh_from_db()
    assert foreign.rate_to_kgs == Decimal("1"), "audit must never modify a row"


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


def test_db_rejects_a_som_row_with_a_bogus_rate_even_via_bulk_create():
    """The rule is backed by a DB CheckConstraint, not just services/admin —
    same discipline as every other money rule here (CLAUDE.md's test list:
    "DB constraints reject bad money/stock even via bulk_create"). This is
    what makes «181 000 сом stored as amount × 88» structurally impossible to
    recreate, by any path, including raw SQL."""
    from django.db import IntegrityError, transaction

    for model, kwargs in (
        (Expense, {"category": Expense.LABOR}),
        (ContractorTransaction, {"kind": ContractorTransaction.ACCRUAL}),
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.bulk_create(
                [
                    model(
                        date=timezone.localdate(),
                        amount=Decimal("181000"),
                        currency="KGS",
                        rate_to_kgs=Decimal("88"),
                        **({"contractor": None} if model is Expense else {}),
                        **kwargs,
                    )
                ]
            )


def test_db_still_allows_a_foreign_row_with_a_real_rate(contractor):
    """The constraint targets base-currency rows only — a genuine foreign
    expense must still convert normally."""
    e = Expense.objects.create(
        date=timezone.localdate(),
        category=Expense.FABRIC,
        amount=Decimal("10"),
        currency="USD",
        rate_to_kgs=Decimal("87.45"),
    )
    assert e.amount_kgs == Decimal("874.50")


# ---------------------------------------------------------------------------
# Navigation: every manufacturing sub-page offers the way back out that
# /orders/ already had («← Все заказы»)
# ---------------------------------------------------------------------------


def test_every_manufacturing_subpage_has_a_back_link(client, django_user_model, contractor):
    """The hub (/manufacturing/) is in the bottom nav, so it needs no back-link
    — exactly like /orders/'s own index. Every page BELOW it does: these are
    dead ends otherwise, and a production/expense form ends in a Save that
    shouldn't be the only way out."""
    owner = django_user_model.objects.create_superuser("nav_owner", "o@e.com", "x" * 12)
    client.force_login(owner)

    for url in (
        f"/manufacturing/contractors/{contractor.pk}/",
        f"/manufacturing/contractors/{contractor.pk}/transaction/add/",
        "/manufacturing/production/add/",
        "/manufacturing/expenses/",
        "/manufacturing/dashboard/",
    ):
        body = client.get(url).content.decode()
        assert 'class="back-link"' in body, f"{url} has no way back"

    # The hub itself deliberately has none (same as /orders/).
    assert 'class="back-link"' not in client.get("/manufacturing/").content.decode()


def test_production_add_back_link_returns_to_the_order_it_came_from(
    client, django_user_model, variant, contractor
):
    """Reached from an order's «Записать производство» (?order_item=N), back
    must return to THAT order — not the hub, which would strand the user a
    click away from the work they were doing."""
    from apps.clients.models import Client as ClientModel
    from apps.orders.services import create_order

    owner = django_user_model.objects.create_superuser("nav2", "o@e.com", "x" * 12)
    client.force_login(owner)

    buyer = ClientModel.objects.create(first_name="Навигация", phone="+996700123999")
    order = create_order(
        client=buyer,
        items=[{"variant": variant, "quantity": 2, "unit_price": Decimal("3200")}],
    )
    item = order.items.first()

    from_order = client.get(f"/manufacturing/production/add/?order_item={item.pk}").content.decode()
    assert f'href="/orders/{order.pk}/"' in from_order, "back must return to the order"

    standalone = client.get("/manufacturing/production/add/").content.decode()
    assert 'href="/manufacturing/"' in standalone, "standalone visit goes back to the hub"


# ---------------------------------------------------------------------------
# Batch production entry (/manufacturing/production/add/): select a product
# once, then record accepted/defect for every size x color cell that
# actually came in, in one submit — record_production_run itself and the
# data model are unchanged, this is only a faster way to call it.
# ---------------------------------------------------------------------------


def _owner_client(client, django_user_model, username):
    owner = django_user_model.objects.create_superuser(username, f"{username}@e.com", "x" * 12)
    client.force_login(owner)
    return owner


@pytest.fixture
def batch_product():
    """One product, three active variants spanning two sizes and two
    colors — enough to exercise a real size x color grid, not the
    single-cell edge case."""
    cat = Category.objects.create(name="Batch Dresses")
    product = Product.objects.create(category=cat, name="Batch Dress")
    v1 = ProductVariant.objects.create(
        product=product,
        sku="BD-S-RED",
        size="S",
        color="red",
        cost_price=Decimal("1000.00"),
        sale_price=Decimal("3000.00"),
    )
    v2 = ProductVariant.objects.create(
        product=product,
        sku="BD-M-RED",
        size="M",
        color="red",
        cost_price=Decimal("1000.00"),
        sale_price=Decimal("3000.00"),
    )
    v3 = ProductVariant.objects.create(
        product=product,
        sku="BD-M-BLUE",
        size="M",
        color="blue",
        cost_price=Decimal("1000.00"),
        sale_price=Decimal("3000.00"),
    )
    return product, v1, v2, v3


def test_batch_post_writes_exactly_one_movement_and_run_per_nonzero_row(
    client, django_user_model, batch_product, contractor
):
    """N cells with a real quantity -> exactly N PRODUCTION_IN movements and
    N ProductionRun rows from one submit — a cell left untouched (empty
    accepted/defect, same as what a real unfilled grid input posts) writes
    nothing for that variant at all."""
    _owner_client(client, django_user_model, "batch_owner1")
    product, v1, v2, v3 = batch_product

    resp = client.post(
        "/manufacturing/production/add/",
        {
            f"accepted_{v1.pk}": "5",
            f"defect_{v1.pk}": "1",
            f"accepted_{v2.pk}": "3",
            f"defect_{v2.pk}": "0",
            f"accepted_{v3.pk}": "",
            f"defect_{v3.pk}": "",
            "contractor": contractor.pk,
            "currency": "KGS",
            "total_cost": "800",
            "note": "",
        },
        follow=True,
    )
    assert resp.status_code == 200
    assert ProductionRun.objects.count() == 2
    assert StockMovement.objects.filter(movement_type=StockMovement.PRODUCTION_IN).count() == 2
    assert not StockMovement.objects.filter(
        variant=v3, movement_type=StockMovement.PRODUCTION_IN
    ).exists()
    assert not ProductionRun.objects.filter(variant=v3).exists()

    run1 = ProductionRun.objects.get(variant=v1)
    assert run1.accepted_qty == 5 and run1.defect_qty == 1
    run2 = ProductionRun.objects.get(variant=v2)
    assert run2.accepted_qty == 3 and run2.defect_qty == 0


def test_batch_zero_qty_cell_writes_nothing(client, django_user_model, batch_product, contractor):
    """A cell explicitly left at 0/0 (typed zero, not just empty) must never
    reach record_production_run — skipped, not recorded as an empty run."""
    _owner_client(client, django_user_model, "batch_owner_zero")
    product, v1, v2, v3 = batch_product

    client.post(
        "/manufacturing/production/add/",
        {
            f"accepted_{v1.pk}": "4",
            f"defect_{v1.pk}": "0",
            f"accepted_{v2.pk}": "0",
            f"defect_{v2.pk}": "0",
            f"accepted_{v3.pk}": "",
            f"defect_{v3.pk}": "",
            "contractor": contractor.pk,
            "currency": "KGS",
            "total_cost": "400",
        },
        follow=True,
    )
    assert ProductionRun.objects.count() == 1
    assert ProductionRun.objects.get().variant_id == v1.pk
    assert not ProductionRun.objects.filter(variant__in=[v2, v3]).exists()
    assert not StockMovement.objects.filter(
        variant__in=[v2, v3], movement_type=StockMovement.PRODUCTION_IN
    ).exists()


def test_batch_failing_row_rolls_back_the_whole_batch(
    client, django_user_model, batch_product, contractor, monkeypatch
):
    """One row failing mid-batch must roll back every row already written in
    THIS submit — record_production_run is called once per cell inside a
    single transaction.atomic(), any exception unwinds all of it, never a
    partial batch."""
    _owner_client(client, django_user_model, "batch_owner_fail")
    product, v1, v2, v3 = batch_product

    from apps.manufacturing import services as mfg_services

    real_record_production_run = mfg_services.record_production_run

    def flaky(*, variant, **kwargs):
        if variant.pk == v2.pk:
            raise ValueError("simulated failure on the second row")
        return real_record_production_run(variant=variant, **kwargs)

    monkeypatch.setattr("apps.manufacturing.views.record_production_run", flaky)

    resp = client.post(
        "/manufacturing/production/add/",
        {
            f"accepted_{v1.pk}": "5",
            f"defect_{v1.pk}": "0",
            f"accepted_{v2.pk}": "3",
            f"defect_{v2.pk}": "0",
            "contractor": contractor.pk,
            "currency": "KGS",
            "total_cost": "800",
        },
    )
    assert resp.status_code == 200  # re-rendered with an error, never a 500
    assert ProductionRun.objects.count() == 0
    assert not StockMovement.objects.filter(movement_type=StockMovement.PRODUCTION_IN).exists()


def test_split_cost_proportionally_sums_back_to_total_exactly():
    """No сом lost or gained to independent per-row rounding, across a range
    of totals/weights including ones that don't divide evenly."""
    from apps.manufacturing.services import split_cost_proportionally

    cases = [
        (Decimal("1000.00"), [3, 3, 4]),
        (Decimal("100.00"), [1, 1, 1]),
        (Decimal("999.99"), [7, 11, 13, 1]),
        (Decimal("50.00"), [0, 5, 0, 5]),
        (Decimal("0.01"), [1, 1, 1]),
    ]
    for total, weights in cases:
        shares = split_cost_proportionally(total, weights)
        assert sum(shares) == total, f"{weights} -> {shares} != {total}"
        for weight, share in zip(weights, shares):
            if weight == 0:
                assert share == Decimal("0")


def test_split_cost_proportionally_zero_total_or_weight_gives_all_zero_shares():
    from apps.manufacturing.services import split_cost_proportionally

    assert split_cost_proportionally(Decimal("0"), [5, 5]) == [Decimal("0"), Decimal("0")]
    assert split_cost_proportionally(Decimal("100"), [0, 0]) == [Decimal("0"), Decimal("0")]


def test_batch_post_cost_split_sums_to_the_entered_total_in_recorded_expenses(
    client, django_user_model, batch_product, contractor
):
    """The same guarantee end-to-end through the view: summing what actually
    landed in the Expense ledger across the whole batch reproduces the
    entered total_cost exactly, even with a 3-way split (1000/11) that
    doesn't divide evenly."""
    _owner_client(client, django_user_model, "batch_owner_split")
    product, v1, v2, v3 = batch_product

    client.post(
        "/manufacturing/production/add/",
        {
            f"accepted_{v1.pk}": "7",
            f"defect_{v1.pk}": "0",
            f"accepted_{v2.pk}": "3",
            f"defect_{v2.pk}": "0",
            f"accepted_{v3.pk}": "1",
            f"defect_{v3.pk}": "0",
            "contractor": contractor.pk,
            "currency": "KGS",
            "total_cost": "1000.00",
        },
        follow=True,
    )
    total_recorded = sum(
        (e.amount for e in Expense.objects.filter(category=Expense.FABRIC)), Decimal("0")
    )
    assert total_recorded == Decimal("1000.00")


def test_batch_order_item_row_still_routes_through_mark_produced(
    client, django_user_model, variant, contractor
):
    """The ?order_item=N entry point must behave exactly as the old single-
    variant form did: the grid pre-fills this line's own shortfall, the row
    routes through mark_produced (produced_qty, status bookkeeping) rather
    than record_production_run directly, and submit redirects back to the
    order — all unchanged now that it's one row inside a batch-shaped form."""
    _owner_client(client, django_user_model, "batch_owner_order")
    from apps.clients.models import Client as ClientModel
    from apps.orders.services import create_order

    buyer = ClientModel.objects.create(first_name="Заказчик", phone="+996700555555")
    order = create_order(
        client=buyer, items=[{"variant": variant, "quantity": 10, "unit_price": Decimal("3200")}]
    )
    item = order.items.first()

    grid_page = client.get(f"/manufacturing/production/add/?order_item={item.pk}").content.decode()
    assert f'value="{item.remaining_to_produce}"' in grid_page

    resp = client.post(
        "/manufacturing/production/add/",
        {
            "order_item": item.pk,
            f"accepted_{variant.pk}": "8",
            f"defect_{variant.pk}": "2",
            "contractor": contractor.pk,
            "currency": "KGS",
            "total_cost": "1000",
        },
    )
    assert resp.status_code == 302
    assert resp.url == f"/orders/{order.pk}/"

    item.refresh_from_db()
    assert item.produced_qty == 8  # defects never count toward the order
    assert ProductionRun.objects.count() == 1
    run = ProductionRun.objects.get(order_item=item)
    assert run.accepted_qty == 8 and run.defect_qty == 2
    assert (
        StockMovement.objects.filter(
            variant=variant, movement_type=StockMovement.PRODUCTION_IN
        ).count()
        == 1
    )
    order.refresh_from_db()
    assert order.status == order.IN_PRODUCTION  # 8 of 10 — not yet fully produced


def test_batch_foreign_cost_without_a_rate_saves_nothing_and_says_so(
    client, django_user_model, batch_product, contractor
):
    """THE DEFECT CLASS CLAUDE.md SWEPT FOR, reaching production cost this
    time: snapshot_rate_to_base falls back to 1.0 by design (a SALE must
    never fail for lack of a rate), but that silently files 1 000 $ of fabric
    as 1 000 сом — understating it ~87x straight into cost_price, and from
    there into COGS and profit, with nothing on screen to show for it. Same
    rule /manufacturing/expenses/ already enforces: save NOTHING."""
    _owner_client(client, django_user_model, "batch_norate")
    product, v1, v2, v3 = batch_product
    assert not ExchangeRate.objects.filter(currency="USD").exists()
    cost_before = v1.cost_price

    resp = client.post(
        "/manufacturing/production/add/",
        {
            f"accepted_{v1.pk}": "10",
            f"defect_{v1.pk}": "0",
            "contractor": contractor.pk,
            "currency": "USD",
            "total_cost": "1000",
        },
        follow=True,
    )
    assert ProductionRun.objects.count() == 0, "nothing may be saved without a rate"
    assert Expense.objects.count() == 0
    assert not StockMovement.objects.filter(movement_type=StockMovement.PRODUCTION_IN).exists()
    v1.refresh_from_db()
    assert v1.cost_price == cost_before, "cost_price must not move on a rejected batch"
    assert "Нет курса" in resp.content.decode()


def test_batch_foreign_cost_with_a_rate_converts_normally(
    client, django_user_model, batch_product, contractor
):
    """The guard above targets a MISSING rate only — a genuine foreign batch
    with a rate on record must still convert at that rate, exactly as a
    single-variant run always did."""
    _owner_client(client, django_user_model, "batch_withrate")
    product, v1, v2, v3 = batch_product
    ExchangeRate.objects.create(currency="USD", rate=Decimal("87.45"), date=timezone.localdate())

    client.post(
        "/manufacturing/production/add/",
        {
            f"accepted_{v1.pk}": "10",
            f"defect_{v1.pk}": "0",
            "contractor": contractor.pk,
            "currency": "USD",
            "total_cost": "1000",
        },
        follow=True,
    )
    v1.refresh_from_db()
    # 1000 USD * 87.45 / 10 accepted = 8745.00 сом/unit — a KGS figure.
    assert v1.cost_price == Decimal("8745.00")


def test_batch_rejects_a_quantity_beyond_the_int_column_and_never_500s(
    client, django_user_model, batch_product, contractor
):
    """accepted_qty is a PositiveIntegerField (int32). A crafted/typo'd value
    above that raises a raw DataError on Postgres — an uncaught 500, not a
    Russian error. MAX_BATCH_QTY rejects it far below the DB ceiling."""
    _owner_client(client, django_user_model, "batch_huge")
    product, v1, v2, v3 = batch_product

    resp = client.post(
        "/manufacturing/production/add/",
        {
            f"accepted_{v1.pk}": "99999999999999",
            f"defect_{v1.pk}": "0",
            "contractor": contractor.pk,
            "currency": "KGS",
            "total_cost": "1000",
        },
    )
    assert resp.status_code == 200, "must be a Russian error, never a 500"
    assert ProductionRun.objects.count() == 0
    assert "Некорректное количество" in resp.content.decode()


def test_batch_rejects_a_negative_cost_instead_of_silently_filing_zero(
    client, django_user_model, batch_product, contractor
):
    """A negative total_cost used to fall through _parse_decimal to None and
    become Decimal(0) — recording the batch with NO cost, leaving cost_price
    stale at the previous run's figure while the user believes they just
    entered this batch's cost."""
    _owner_client(client, django_user_model, "batch_negcost")
    product, v1, v2, v3 = batch_product

    resp = client.post(
        "/manufacturing/production/add/",
        {
            f"accepted_{v1.pk}": "5",
            f"defect_{v1.pk}": "0",
            "contractor": contractor.pk,
            "currency": "KGS",
            "total_cost": "-500",
        },
    )
    assert resp.status_code == 200
    assert ProductionRun.objects.count() == 0
    assert "Некорректная стоимость" in resp.content.decode()


def test_batch_without_a_cost_is_allowed_and_leaves_cost_price_alone(
    client, django_user_model, batch_product, contractor
):
    """A blank cost stays legitimate — production recorded now, cost data
    entered later. record_production_run's own rule: a costless run leaves
    cost_price untouched rather than zeroing it out."""
    _owner_client(client, django_user_model, "batch_nocost")
    product, v1, v2, v3 = batch_product
    cost_before = v1.cost_price

    client.post(
        "/manufacturing/production/add/",
        {
            f"accepted_{v1.pk}": "5",
            f"defect_{v1.pk}": "0",
            "contractor": contractor.pk,
            "currency": "KGS",
            "total_cost": "",
        },
        follow=True,
    )
    assert ProductionRun.objects.count() == 1
    v1.refresh_from_db()
    assert v1.cost_price == cost_before


def test_batch_rejects_variants_from_two_different_products(
    client, django_user_model, batch_product, contractor
):
    """The grid only ever renders ONE product's variants, so a POST mixing
    two products is necessarily crafted — and would silently split a batch
    cost across unrelated goods. Rejected wholesale, nothing written."""
    _owner_client(client, django_user_model, "batch_twoprod")
    product, v1, v2, v3 = batch_product
    other = Product.objects.create(category=product.category, name="Other Batch Dress")
    other_v = ProductVariant.objects.create(
        product=other,
        sku="OBD-S-GRN",
        size="S",
        color="green",
        cost_price=Decimal("1000.00"),
        sale_price=Decimal("3000.00"),
    )

    resp = client.post(
        "/manufacturing/production/add/",
        {
            f"accepted_{v1.pk}": "5",
            f"defect_{v1.pk}": "0",
            f"accepted_{other_v.pk}": "5",
            f"defect_{other_v.pk}": "0",
            "contractor": contractor.pk,
            "currency": "KGS",
            "total_cost": "1000",
        },
    )
    assert resp.status_code == 200
    assert ProductionRun.objects.count() == 0


def test_batch_missing_contractor_is_an_error_not_a_500(client, django_user_model, batch_product):
    """An empty <select> posts contractor="" — get_object_or_404 only maps
    DoesNotExist to a 404, so a bare int("") would have 500'd."""
    _owner_client(client, django_user_model, "batch_nocontractor")
    product, v1, v2, v3 = batch_product

    resp = client.post(
        "/manufacturing/production/add/",
        {
            f"accepted_{v1.pk}": "5",
            f"defect_{v1.pk}": "0",
            "contractor": "",
            "currency": "KGS",
            "total_cost": "100",
        },
    )
    assert resp.status_code == 200
    assert ProductionRun.objects.count() == 0
    assert "подрядчика" in resp.content.decode()


def test_batch_endpoints_are_owner_only(client, django_user_model, batch_product):
    """Every NEW surface this feature added — the search partial and the grid
    partial, not just the page — is Owner-only, checked server-side. An
    Editor hitting the HTMX endpoints directly gets 403, never the catalog."""
    from django.contrib.auth.models import Group

    from apps.core.permissions import EDITOR

    product, v1, v2, v3 = batch_product
    editor_group, _created = Group.objects.get_or_create(name=EDITOR)
    editor = django_user_model.objects.create_user(
        "batch_editor", "be@e.com", "x" * 12, is_staff=True
    )
    editor.groups.add(editor_group)
    client.force_login(editor)

    for url in (
        "/manufacturing/production/add/",
        "/manufacturing/production/search/",
        f"/manufacturing/production/grid/{product.pk}/",
    ):
        assert client.get(url).status_code == 403, f"Editor should be denied {url}"

    # ...and the write path too, not just the reads.
    assert (
        client.post(
            "/manufacturing/production/add/",
            {f"accepted_{v1.pk}": "5", f"defect_{v1.pk}": "0", "contractor": "1"},
        ).status_code
        == 403
    )
    assert ProductionRun.objects.count() == 0


def test_picking_a_product_replaces_the_picker_instead_of_stacking_under_it(
    client, django_user_model, batch_product
):
    """The catalog list must be GONE once a product is chosen, not left
    expanded above the grid — with 30 products rendered on load, leaving it
    in place means scrolling past the whole catalog to reach the grid you
    just selected. The picker and the grid share one swap target."""
    _owner_client(client, django_user_model, "batch_picker")
    product, v1, v2, v3 = batch_product

    picker = client.get("/manufacturing/production/add/").content.decode()
    assert 'id="production-search"' in picker, "step 1 offers the search"
    assert 'id="production-batch-form"' not in picker, "no grid before a product is picked"

    grid = client.get(f"/manufacturing/production/grid/{product.pk}/").content.decode()
    assert 'id="production-batch-form"' in grid, "step 2 is the grid"
    assert 'id="production-search"' not in grid, "the catalog list must not survive the swap"
    assert "Другой товар" in grid, "and there must be a way back to pick a different product"


def test_order_item_entry_skips_the_picker_entirely(client, django_user_model, variant, contractor):
    """Arriving from an order already knows the product — showing a catalog
    search first would be a pointless step, and picking a different product
    there would contradict the order that sent you."""
    _owner_client(client, django_user_model, "batch_orderpicker")
    from apps.clients.models import Client as ClientModel
    from apps.orders.services import create_order

    buyer = ClientModel.objects.create(first_name="Прямо", phone="+996700555777")
    order = create_order(
        client=buyer, items=[{"variant": variant, "quantity": 6, "unit_price": Decimal("3200")}]
    )
    item = order.items.first()

    body = client.get(f"/manufacturing/production/add/?order_item={item.pk}").content.decode()
    assert 'id="production-batch-form"' in body
    assert 'id="production-search"' not in body
    assert "Другой товар" not in body, "the order fixes the product — no switching away from it"


def test_batch_form_query_count_does_not_grow_with_the_catalog(client, django_user_model):
    """Same N+1 discipline CLAUDE.md holds the POS product grid to («grid ≤4
    queries at 10 and 500 rows»), applied to this form's two HTMX endpoints:
    the query count must be IDENTICAL at a small and a large catalog, so a
    growing product list can never quietly turn the picker into a per-row
    query storm."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    owner = django_user_model.objects.create_superuser("perf_owner", "o@e.com", "x" * 12)
    client.force_login(owner)
    cat = Category.objects.create(name="PerfCat")

    def build(n_products, n_variants, tag):
        big = Product.objects.create(category=cat, name=f"Perf {tag} big")
        ProductVariant.objects.bulk_create(
            [
                ProductVariant(
                    product=big,
                    sku=f"PF-{tag}-{i}",
                    size=f"S{i // 10}",
                    color=f"C{i % 10}",
                    cost_price=Decimal("100"),
                    sale_price=Decimal("500"),
                )
                for i in range(n_variants)
            ]
        )
        for i in range(n_products):
            p = Product.objects.create(category=cat, name=f"Perf {tag} {i}")
            ProductVariant.objects.create(
                product=p,
                sku=f"O-{tag}-{i}",
                size="S",
                color="C",
                cost_price=Decimal("100"),
                sale_price=Decimal("500"),
            )
        return big

    def measure(product):
        counts = {}
        for label, url in (
            ("search", "/manufacturing/production/search/"),
            ("search_q", "/manufacturing/production/search/?q=Perf"),
            ("grid", f"/manufacturing/production/grid/{product.pk}/"),
        ):
            with CaptureQueriesContext(connection) as ctx:
                assert client.get(url).status_code == 200
            counts[label] = len(ctx.captured_queries)
        return counts

    small = measure(build(5, 5, "small"))
    large = measure(build(300, 200, "large"))

    assert small == large, f"query count grew with the catalog — N+1 crept in: {small} -> {large}"
    # A hard ceiling too, so the flat-but-huge case can't slip through either.
    assert large["grid"] <= 8, large
    assert large["search"] <= 6, large
