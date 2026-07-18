import hashlib
import hmac
import json
from datetime import date
from decimal import Decimal

import pytest
import requests
from django.contrib.admin.sites import site
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory
from django.utils import timezone

from apps.clients.models import Client, Interaction
from apps.clients.services import client_debt, debtors_report_rows, log_whatsapp_interaction
from apps.core.currency import from_base, get_rate, to_base
from apps.core.management.commands.send_daily_report import (
    _debts_rows,
    _sales_rows,
    _stock_rows,
    _unreviewed_rows,
)
from apps.core.models import ExchangeRate
from apps.core.permissions import EDITOR, VIEWER
from apps.inventory.models import Category, Product, ProductVariant, StockMovement
from apps.inventory.services import add_movement, adjust_to_count
from apps.reports.models import DailyReview
from apps.sales.models import Payment, SaleItem, SaleOrder
from apps.sales.services import (
    cancel_sale,
    confirm_sale,
    mark_fully_paid,
    record_payment,
    void_payment,
)

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
        cost_price=Decimal("1500.00"),
        sale_price=Decimal("3200.00"),
    )


def test_stock_is_sum_of_movements(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    add_movement(variant, StockMovement.WRITEOFF_OUT, 2)
    assert variant.stock == 8


def test_confirm_sale_decrements_stock_and_sets_total(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    client = Client.objects.create(first_name="Aisha", phone="+996700000001")
    order = SaleOrder.objects.create(client=client, channel=SaleOrder.INSTAGRAM)
    SaleItem.objects.create(order=order, variant=variant, quantity=3, unit_price=Decimal("3200"))

    confirm_sale(order)
    order.refresh_from_db()
    assert order.status == SaleOrder.CONFIRMED
    assert order.total == Decimal("9600")
    assert variant.stock == 7


def test_cannot_oversell(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 2)
    order = SaleOrder.objects.create()
    SaleItem.objects.create(order=order, variant=variant, quantity=5, unit_price=Decimal("3200"))
    with pytest.raises(ValidationError):
        confirm_sale(order)
    assert variant.stock == 2  # nothing was written off


def test_cancel_returns_stock(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    order = SaleOrder.objects.create()
    SaleItem.objects.create(order=order, variant=variant, quantity=4, unit_price=Decimal("3200"))
    confirm_sale(order)
    assert variant.stock == 1
    cancel_sale(order)
    assert variant.stock == 5


def test_debt_is_sales_minus_payments(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    client = Client.objects.create(first_name="Meerim", phone="+996700000002")
    order = SaleOrder.objects.create(client=client)
    SaleItem.objects.create(order=order, variant=variant, quantity=2, unit_price=Decimal("3200"))
    confirm_sale(order)
    Payment.objects.create(client=client, order=order, amount=Decimal("2000"))
    assert client_debt(client) == {"KGS": Decimal("4400")}


def test_debt_is_tracked_independently_per_currency(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    client = Client.objects.create(first_name="Aidai", phone="+996700000010")
    kgs_order = SaleOrder.objects.create(client=client, currency="KGS")
    SaleItem.objects.create(
        order=kgs_order, variant=variant, quantity=1, unit_price=Decimal("3200")
    )
    confirm_sale(kgs_order)
    usd_order = SaleOrder.objects.create(client=client, currency="USD")
    SaleItem.objects.create(order=usd_order, variant=variant, quantity=1, unit_price=Decimal("40"))
    confirm_sale(usd_order)

    # A USD payment only offsets the USD debt, never the KGS one.
    Payment.objects.create(client=client, order=usd_order, amount=Decimal("40"), currency="USD")

    debts = client_debt(client)
    assert debts == {"KGS": Decimal("3200")}


def test_adjustment_requires_reason_and_writes_diff(variant, django_user_model):
    user = django_user_model.objects.create_user("owner", password="x" * 12)
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    with pytest.raises(ValueError):
        adjust_to_count(variant, 7, user, reason="")
    adjust_to_count(variant, 7, user, reason="inventory count")
    assert variant.stock == 7


def test_editor_cannot_see_cost_price_field(variant, django_user_model):
    call_command("setup_roles")
    user = django_user_model.objects.create_user("editor1", password="x" * 12, is_staff=True)
    user.groups.add(Group.objects.get(name=EDITOR))
    request = RequestFactory().get("/")
    request.user = user
    model_admin = site._registry[ProductVariant]
    assert "cost_price" not in model_admin.get_fields(request, variant)


def test_viewer_has_no_change_permission(django_user_model):
    call_command("setup_roles")
    user = django_user_model.objects.create_user("viewer1", password="x" * 12, is_staff=True)
    user.groups.add(Group.objects.get(name=VIEWER))
    request = RequestFactory().get("/")
    request.user = user
    model_admin = site._registry[ProductVariant]
    assert model_admin.has_view_permission(request) is True
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


def test_setup_roles_excludes_core_and_wa_apps():
    call_command("setup_roles")
    editor = Group.objects.get(name=EDITOR)
    viewer = Group.objects.get(name=VIEWER)
    assert not editor.permissions.filter(
        content_type__app_label__in=["core", "wa", "auth"]
    ).exists()
    assert not viewer.permissions.filter(
        content_type__app_label__in=["core", "wa", "auth"]
    ).exists()
    assert not editor.permissions.filter(codename__startswith="delete_").exists()


def test_setup_roles_keeps_exchange_rates_superuser_only():
    # ExchangeRate lives under apps.core (Система) — Editor/Viewer never get it,
    # matching "exchange rates" being listed as a Система-only model.
    call_command("setup_roles")
    editor = Group.objects.get(name=EDITOR)
    viewer = Group.objects.get(name=VIEWER)
    assert not editor.permissions.filter(codename__endswith="_exchangerate").exists()
    assert not viewer.permissions.filter(codename__endswith="_exchangerate").exists()


def test_editor_can_view_daily_review_but_not_act_on_it(django_user_model):
    # DB permissions alone don't matter here — the ModelAdmin itself blocks
    # editing/reviewing for non-superusers regardless of what's granted.
    call_command("setup_roles")
    editor = Group.objects.get(name=EDITOR)
    assert editor.permissions.filter(codename="view_dailyreview").exists()

    user = django_user_model.objects.create_user("editor2", password="x" * 12, is_staff=True)
    user.groups.add(editor)
    request = RequestFactory().get("/")
    request.user = user
    model_admin = site._registry[DailyReview]
    assert model_admin.has_view_permission(request) is True
    assert model_admin.has_change_permission(request) is False
    assert "mark_reviewed" not in model_admin.get_actions(request)


def test_stock_movement_and_standalone_payment_hidden_from_sidebar():
    from apps.inventory.models import StockMovement as SM

    request = RequestFactory().get("/")
    assert site._registry[SM].has_module_permission(request) is False
    assert site._registry[Payment].has_module_permission(request) is False


def test_whatsapp_message_auto_creates_client_and_interaction():
    client = log_whatsapp_interaction("+996700000099", "hello")
    assert client.source == Client.WHATSAPP
    assert client.interactions.count() == 1
    assert client.interactions.first().kind == Interaction.MESSAGE

    # A second message from the same number reuses the client, doesn't duplicate it.
    log_whatsapp_interaction("+996700000099", "again")
    assert Client.objects.filter(phone="+996700000099").count() == 1
    assert client.interactions.count() == 2


def test_daily_report_rows_are_russian_and_reflect_current_data(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    client = Client.objects.create(first_name="Aiperi", phone="+996700000003")
    order = SaleOrder.objects.create(client=client, channel=SaleOrder.SHOP)
    SaleItem.objects.create(order=order, variant=variant, quantity=2, unit_price=Decimal("3200"))
    confirm_sale(order)
    Payment.objects.create(client=client, order=order, amount=Decimal("1000"))

    sales = _sales_rows()
    assert sales[0][0] == "Время"
    assert any(row[1] == "Aiperi" for row in sales[1:-1])

    stock = _stock_rows()
    assert stock[0][0] == "Артикул"
    assert any(row[0] == variant.sku and row[4] == 8 for row in stock[1:])

    debts = _debts_rows()
    assert debts[0][0] == "Имя"
    assert any(row[0] == "Aiperi" and row[2] == "5400" and row[3] == "KGS" for row in debts[1:])


def test_unreviewed_rows_lists_todays_unreviewed_payments(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    client = Client.objects.create(first_name="Nurgul", phone="+996700000011")
    order = SaleOrder.objects.create(client=client)
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)
    Payment.objects.create(client=client, order=order, amount=Decimal("3200"))

    rows = _unreviewed_rows()
    assert rows[0][0] == "Время"
    assert any(row[1] == "Nurgul" for row in rows[1:])


def test_send_daily_report_command_skips_network_when_unconfigured(capsys):
    call_command("send_daily_report")
    out = capsys.readouterr().out.lower()
    assert "skipped" in out


def test_payment_status_reflects_payments(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    c = Client.objects.create(first_name="Nur", phone="+996700000004")
    order = SaleOrder.objects.create(client=c)
    SaleItem.objects.create(order=order, variant=variant, quantity=2, unit_price=Decimal("3200"))
    confirm_sale(order)
    order.refresh_from_db()

    assert order.total == Decimal("6400")
    assert order.payment_status == SaleOrder.UNPAID
    assert order.balance == Decimal("6400")

    Payment.objects.create(client=c, order=order, amount=Decimal("2400"))
    assert order.payment_status == SaleOrder.PARTIAL
    assert order.balance == Decimal("4000")

    Payment.objects.create(client=c, order=order, amount=Decimal("4000"))
    assert order.payment_status == SaleOrder.PAID
    assert order.balance == Decimal("0")


def test_pending_order_has_no_balance(variant):
    order = SaleOrder.objects.create()
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    # Not approved yet — a pending order owes nothing and reads as unpaid.
    assert order.balance == Decimal("0")
    assert order.payment_status == SaleOrder.UNPAID


def test_mismatched_currency_payment_does_not_count_toward_order_balance(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    c = Client.objects.create(first_name="Aliya", phone="+996700000012")
    order = SaleOrder.objects.create(client=c, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)
    # A USD payment against a KGS order doesn't pay it down.
    Payment.objects.create(client=c, order=order, amount=Decimal("40"), currency="USD")
    assert order.paid_amount == Decimal("0")
    assert order.payment_status == SaleOrder.UNPAID


def test_stats_and_download_require_superuser(client, django_user_model):
    # Anonymous -> redirected to login (not reachable by guessing the URL).
    assert client.get("/stats/").status_code == 302
    assert client.get("/stats/download/?format=csv&sheet=sales").status_code == 302

    # Staff but not superuser -> forbidden.
    staff = django_user_model.objects.create_user("staff1", password="x" * 12, is_staff=True)
    client.force_login(staff)
    assert client.get("/stats/").status_code == 403
    assert client.get("/stats/download/?format=xlsx").status_code == 403

    # Superuser -> OK, and the CSV download carries the UTF-8 BOM.
    root = django_user_model.objects.create_superuser("root1", "r@e.com", "x" * 12)
    client.force_login(root)
    assert client.get("/stats/").status_code == 200
    resp = client.get("/stats/download/?format=csv&sheet=sales")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")
    assert resp.content[:3] == b"\xef\xbb\xbf"


def test_currency_conversion_uses_dated_rate(settings):
    settings.CURRENCY = "KGS"
    ExchangeRate.objects.create(currency="USD", date=date(2026, 7, 10), rate=Decimal("89"))
    ExchangeRate.objects.create(currency="USD", date=date(2026, 7, 12), rate=Decimal("90"))

    # Base currency is always 1:1, no rate needed, in either direction.
    assert to_base(Decimal("9000"), "KGS", date(2026, 7, 13)) == Decimal("9000.00")
    assert from_base(Decimal("9000"), "KGS", date(2026, 7, 13)) == Decimal("9000.00")

    # Uses the most recent rate on or before the date (Jul 12 rate on Jul 13).
    assert get_rate("USD", date(2026, 7, 13)) == Decimal("90")
    # from_base: KGS -> USD divides by the rate.
    assert from_base(Decimal("9000"), "USD", date(2026, 7, 13)) == Decimal("100.00")
    # to_base: USD -> KGS multiplies by the rate.
    assert to_base(Decimal("100"), "USD", date(2026, 7, 13)) == Decimal("9000.00")

    # An earlier date picks the earlier rate.
    assert from_base(Decimal("8900"), "USD", date(2026, 7, 11)) == Decimal("100.00")

    # No rate on/before the date -> None (caller falls back to base).
    assert from_base(Decimal("9000"), "USD", date(2026, 7, 1)) is None
    assert to_base(Decimal("100"), "USD", date(2026, 7, 1)) is None


_NBKR_SAMPLE = (
    b'<?xml version="1.0" encoding="windows-1251" ?>'
    b'<CurrencyRates Name="Daily Exchange Rates" Date="17.07.2026">'
    b'<Currency ISOCode="USD"><Nominal>1</Nominal><Value>87,4500</Value></Currency>'
    b'<Currency ISOCode="RUB"><Nominal>1</Nominal><Value>1,1239</Value></Currency>'
    b'<Currency ISOCode="EUR"><Nominal>1</Nominal><Value>100,0000</Value></Currency>'
    b"</CurrencyRates>"
)


class _FakeResp:
    content = _NBKR_SAMPLE

    def raise_for_status(self):
        pass


def test_fetch_rates_parses_nbkr_and_skips_unconfigured(monkeypatch, settings):
    settings.CURRENCY = "KGS"
    monkeypatch.setattr(
        "apps.core.management.commands.fetch_rates.requests.get", lambda *a, **k: _FakeResp()
    )
    call_command("fetch_rates")
    today = timezone.localdate()
    assert ExchangeRate.objects.get(currency="USD", date=today).rate == Decimal("87.45")
    assert ExchangeRate.objects.get(currency="RUB", date=today).rate == Decimal("1.1239")
    # EUR isn't a configured currency — it must be ignored, not stored.
    assert not ExchangeRate.objects.filter(currency="EUR").exists()


def test_fetch_rates_never_overwrites_a_manual_override(monkeypatch, settings):
    settings.CURRENCY = "KGS"
    today = timezone.localdate()
    ExchangeRate.objects.create(
        currency="USD", date=today, rate=Decimal("99.99"), source=ExchangeRate.MANUAL
    )
    monkeypatch.setattr(
        "apps.core.management.commands.fetch_rates.requests.get", lambda *a, **k: _FakeResp()
    )
    call_command("fetch_rates")
    kept = ExchangeRate.objects.get(currency="USD", date=today)
    assert kept.rate == Decimal("99.99") and kept.source == ExchangeRate.MANUAL


def test_fetch_rates_survives_network_failure(monkeypatch, settings):
    settings.CURRENCY = "KGS"

    def boom(*a, **k):
        raise requests.RequestException("network down")

    monkeypatch.setattr("apps.core.management.commands.fetch_rates.requests.get", boom)
    call_command("fetch_rates")  # must not raise
    assert not ExchangeRate.objects.exists()  # nothing written, last known kept


def test_restock_reply_lists_only_low_variants(variant):
    from apps.wa.replies import restock_reply

    variant.low_stock_threshold = 3
    variant.save(update_fields=["low_stock_threshold"])
    add_movement(variant, StockMovement.PRODUCTION_IN, 2)  # 2 <= 3 -> low
    reply = restock_reply()
    assert variant.sku in reply and "Low stock" in reply

    add_movement(variant, StockMovement.PRODUCTION_IN, 10)  # now 12 > 3 -> healthy
    assert variant.sku not in restock_reply()


def test_bot_stock_replies_do_not_n_plus_one(variant):
    # /restock and /stock answer from ONE annotated query however many variants
    # match — the old per-variant .stock property loop was O(catalog) queries.
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.inventory.services import low_stock_variants
    from apps.wa.replies import stock_reply

    cat = Category.objects.create(name="BotPerf")
    for i in range(40):
        p = Product.objects.create(category=cat, name=f"Платье Bot{i}")
        ProductVariant.objects.create(
            product=p, sku=f"BOT-{i:03d}", cost_price=Decimal("1"), sale_price=Decimal("2")
        )

    with CaptureQueriesContext(connection) as ctx:
        low = low_stock_variants()
    assert len(low) >= 40  # all fresh variants have stock 0 <= threshold 2
    assert len(ctx.captured_queries) == 1

    with CaptureQueriesContext(connection) as ctx:
        text = stock_reply("Платье Bot")
    assert "BOT-000" in text
    assert len(ctx.captured_queries) == 1


def test_approve_paid_in_full_records_payment(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    c = Client.objects.create(first_name="Gulnaz", phone="+996700000005")
    order = SaleOrder.objects.create(client=c)
    SaleItem.objects.create(order=order, variant=variant, quantity=2, unit_price=Decimal("3200"))

    confirm_sale(order)
    record_payment(order, order.total)  # what the "paid in full" choice does
    order.refresh_from_db()
    assert order.payment_status == SaleOrder.PAID
    assert order.balance == Decimal("0")
    assert order.payments.get().currency == order.currency


def test_mark_fully_paid_settles_balance(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    c = Client.objects.create(first_name="Bermet", phone="+996700000006")
    order = SaleOrder.objects.create(client=c)
    SaleItem.objects.create(order=order, variant=variant, quantity=2, unit_price=Decimal("3200"))
    confirm_sale(order)
    Payment.objects.create(client=c, order=order, amount=Decimal("2000"))  # partial

    mark_fully_paid(order)
    assert order.balance == Decimal("0")
    assert order.payment_status == SaleOrder.PAID


def test_walkin_order_records_no_payment(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    order = SaleOrder.objects.create()  # no client
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)
    # Walk-in has no client, so there is nothing to owe and no payment is created.
    assert record_payment(order, order.total) is None
    assert order.payments.count() == 0


def test_void_payment_creates_reversing_entry_not_a_delete(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    c = Client.objects.create(first_name="Zarina", phone="+996700000013")
    order = SaleOrder.objects.create(client=c)
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)
    payment = Payment.objects.create(client=c, order=order, amount=Decimal("3200"))

    reversal = void_payment(payment)
    assert reversal.amount == Decimal("-3200")
    assert reversal.reversed_payment_id == payment.pk
    assert Payment.objects.filter(pk=payment.pk).exists()  # never deleted
    order.refresh_from_db()
    assert order.paid_amount == Decimal("0")  # net effect: fully reversed


def test_void_reviewed_payment_requires_superuser(variant, django_user_model):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    c = Client.objects.create(first_name="Elmira", phone="+996700000014")
    order = SaleOrder.objects.create(client=c)
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)
    payment = Payment.objects.create(client=c, order=order, amount=Decimal("3200"))
    payment.reviewed = True
    payment.save(update_fields=["reviewed"])

    editor = django_user_model.objects.create_user("editor3", password="x" * 12, is_staff=True)
    with pytest.raises(ValidationError):
        void_payment(payment, user=editor)

    root = django_user_model.objects.create_superuser("root2", "r2@e.com", "x" * 12)
    reversal = void_payment(payment, user=root)  # does not raise
    assert reversal.reversed_payment_id == payment.pk


def test_debtors_report_rows_group_by_currency(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    client = Client.objects.create(first_name="Asel", phone="+996700000015")
    order = SaleOrder.objects.create(client=client, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)

    rows = debtors_report_rows()
    matching = [r for r in rows if r[0].pk == client.pk]
    assert matching == [(client, "KGS", Decimal("3200"), None)]


# ---- Phase 2: /pos/ routing, idempotency, permissions, draft lifecycle -----


def test_confirm_sale_is_idempotent_against_double_call(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    order = SaleOrder.objects.create()
    SaleItem.objects.create(order=order, variant=variant, quantity=3, unit_price=Decimal("3200"))
    confirm_sale(order)
    assert variant.stock == 7

    # A second confirm on the same (now-approved) order must not decrement
    # stock again — the guarantee behind "double-tap produces exactly one sale".
    with pytest.raises(ValidationError):
        confirm_sale(order)
    assert variant.stock == 7


def test_pos_root_redirects_by_auth_state(client, django_user_model):
    resp = client.get("/", follow=True)
    assert resp.redirect_chain[-1][0].startswith("/login/")

    call_command("setup_roles")
    editor = django_user_model.objects.create_user("editor10", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)
    resp = client.get("/", follow=True)
    assert "/pos/sale/" in resp.redirect_chain[-1][0]


def test_pos_admin_redirects_to_shared_login_not_its_own_form(client):
    resp = client.get("/panel/", follow=True)
    assert resp.redirect_chain[-1][0].startswith("/login/")


def test_pos_viewer_blocked_from_selling_but_can_read(client, django_user_model):
    call_command("setup_roles")
    editor = django_user_model.objects.create_user("editor9", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    viewer = django_user_model.objects.create_user("viewer9", password="x" * 12, is_staff=True)
    viewer.groups.add(Group.objects.get(name=VIEWER))

    client.force_login(viewer)
    # /pos/ is LOGIN_REDIRECT_URL for everyone, Viewer included — it must not
    # be a dead end, so it routes them to Сегодня instead of 403ing.
    resp = client.get("/pos/")
    assert resp.status_code == 302
    assert resp.url == "/pos/today/"
    assert client.get("/pos/today/").status_code == 200
    assert client.get("/pos/clients/").status_code == 200
    # Direct sale-editing URLs stay blocked even though / no longer 403s.
    assert client.get("/pos/sale/1/").status_code in (403, 404)

    client.force_login(editor)
    assert client.get("/pos/").status_code == 302  # lands on a fresh draft
    assert client.get("/pos/today/").status_code == 200


def test_pos_new_sale_flow_is_idempotent_end_to_end(client, django_user_model, variant):
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    editor = django_user_model.objects.create_user("editor11", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])

    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 2})
    resp = client.post(
        f"/pos/sale/{order_id}/confirm/", {"amount": "6400", "currency": "KGS", "method": "cash"}
    )
    assert resp.status_code == 302
    assert resp.url == f"/pos/sale/{order_id}/result/"

    order = SaleOrder.objects.get(pk=order_id)
    assert order.status == SaleOrder.CONFIRMED
    assert order.total == Decimal("6400")
    variant.refresh_from_db()
    assert variant.stock == 8

    # Double-submit: the exact same POST again must not create a second sale
    # or decrement stock again — it just lands back on the same result page.
    resp2 = client.post(
        f"/pos/sale/{order_id}/confirm/", {"amount": "6400", "currency": "KGS", "method": "cash"}
    )
    assert resp2.status_code == 302
    assert resp2.url == f"/pos/sale/{order_id}/result/"
    variant.refresh_from_db()
    assert variant.stock == 8
    assert SaleOrder.objects.filter(status=SaleOrder.CONFIRMED).count() == 1


def test_pos_draft_survives_reload(client, django_user_model, variant):
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    editor = django_user_model.objects.create_user("editor12", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})

    # Simulate a dropped connection / reload: hitting /pos/ again reuses the
    # same still-open draft instead of losing the basket.
    resp2 = client.get("/pos/")
    assert resp2.url == f"/pos/sale/{order_id}/"
    order = SaleOrder.objects.get(pk=order_id)
    assert order.items.count() == 1


def test_pos_oversell_shows_error_and_keeps_basket(client, django_user_model, variant):
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 2)
    editor = django_user_model.objects.create_user("editor13", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 5})
    resp = client.post(
        f"/pos/sale/{order_id}/confirm/", {"amount": "0", "currency": "KGS", "method": "cash"}
    )
    # Redirects back to the (GET-able) sale page with the error as a message,
    # rather than rendering directly — a refresh must not prompt "resubmit
    # form?", and the offline-safe fetch() submit needs every outcome of this
    # POST to end in a redirect it can safely follow.
    assert resp.status_code == 302
    assert resp.url == f"/pos/sale/{order_id}/"
    page = client.get(resp.url)
    assert "Недостаточно" in page.content.decode()
    order = SaleOrder.objects.get(pk=order_id)
    assert order.status == SaleOrder.DRAFT
    assert order.items.count() == 1  # basket kept intact
    variant.refresh_from_db()
    assert variant.stock == 2  # nothing written off


def test_pos_cancel_view_returns_stock(client, django_user_model, variant):
    # Exercises the /pos/ cancel *view* over HTTP, not just the cancel_sale
    # service — an Editor cancelling their own same-day sale should 302 to the
    # result page and return the stock. (A regression here once slipped past
    # the service-only tests.)
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    editor = django_user_model.objects.create_user("editor14", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 2})
    client.post(
        f"/pos/sale/{order_id}/confirm/", {"amount": "6400", "currency": "KGS", "method": "cash"}
    )
    variant.refresh_from_db()
    assert variant.stock == 3

    resp = client.post(f"/pos/sale/{order_id}/cancel/")
    assert resp.status_code == 302
    assert resp.url == f"/pos/sale/{order_id}/result/"
    order = SaleOrder.objects.get(pk=order_id)
    assert order.status == SaleOrder.CANCELLED
    variant.refresh_from_db()
    assert variant.stock == 5  # returned to stock


def test_pos_share_receipt_redirects_to_whatsapp_and_logs(client, django_user_model, variant):
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    editor = django_user_model.objects.create_user("editor15", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    cust = Client.objects.create(first_name="Aiperi", phone="+996700123456")
    order = SaleOrder.objects.create(client=cust, created_by=editor)
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order, user=editor)

    resp = client.get(f"/pos/sale/{order.pk}/receipt/")
    assert resp.status_code == 302
    assert resp.url.startswith("https://wa.me/996700123456?text=")
    assert cust.interactions.filter(kind=Interaction.MESSAGE).count() == 1


def test_pos_debt_reminder_redirects_to_whatsapp_and_logs(client, django_user_model, variant):
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    editor = django_user_model.objects.create_user("editor16", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    cust = Client.objects.create(first_name="Nurgul", phone="+996700999888")
    order = SaleOrder.objects.create(client=cust, created_by=editor)
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order, user=editor)  # unpaid -> client now owes 3200 KGS

    resp = client.get(f"/pos/clients/{cust.pk}/remind/")
    assert resp.status_code == 302
    assert resp.url.startswith("https://wa.me/996700999888?text=")
    assert cust.interactions.filter(kind=Interaction.MESSAGE).count() == 1

    # A client with no debt gets bounced back, nothing logged.
    paid = Client.objects.create(first_name="Zarina", phone="+996700111000")
    resp2 = client.get(f"/pos/clients/{paid.pk}/remind/")
    assert resp2.status_code == 302
    assert resp2.url == f"/pos/clients/{paid.pk}/"


def test_partial_return_restocks_and_reduces_total(variant):
    from apps.sales.services import return_items

    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    cust = Client.objects.create(first_name="Meerim", phone="+996700222111")
    order = SaleOrder.objects.create(client=cust)
    item = SaleItem.objects.create(
        order=order, variant=variant, quantity=3, unit_price=Decimal("3200")
    )
    confirm_sale(order)
    assert variant.stock == 7 and order.total == Decimal("9600")

    return_items(order, {item.pk: 1}, user=None)
    order.refresh_from_db()
    variant.refresh_from_db()
    assert variant.stock == 8  # one came back
    assert order.total == Decimal("6400")  # 2 remaining × 3200
    assert order.status == SaleOrder.CONFIRMED
    # Debt follows the reduced total automatically.
    assert client_debt(cust) == {"KGS": Decimal("6400")}


def test_full_return_cancels_the_order(variant):
    from apps.sales.services import return_items

    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    order = SaleOrder.objects.create()
    item = SaleItem.objects.create(
        order=order, variant=variant, quantity=2, unit_price=Decimal("3200")
    )
    confirm_sale(order)
    return_items(order, {item.pk: 2}, user=None)
    order.refresh_from_db()
    variant.refresh_from_db()
    assert order.status == SaleOrder.CANCELLED
    assert variant.stock == 10  # all back


def test_return_rejects_more_than_sold(variant):
    from apps.sales.services import return_items

    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    order = SaleOrder.objects.create()
    item = SaleItem.objects.create(
        order=order, variant=variant, quantity=2, unit_price=Decimal("3200")
    )
    confirm_sale(order)
    with pytest.raises(ValidationError):
        return_items(order, {item.pk: 5}, user=None)
    variant.refresh_from_db()
    assert variant.stock == 8  # unchanged — the whole return rolled back


def test_units_sold_by_variant_counts_only_confirmed(variant):
    from apps.sales.services import units_sold_by_variant

    add_movement(variant, StockMovement.PRODUCTION_IN, 20)
    # A confirmed sale of 4 counts; a draft of 5 does not.
    o1 = SaleOrder.objects.create()
    SaleItem.objects.create(order=o1, variant=variant, quantity=4, unit_price=Decimal("3200"))
    confirm_sale(o1)
    o2 = SaleOrder.objects.create()  # left as draft
    SaleItem.objects.create(order=o2, variant=variant, quantity=5, unit_price=Decimal("3200"))

    assert units_sold_by_variant(30) == {variant.pk: 4}


def test_campaign_audience_requires_consent_and_chat_id():
    from apps.campaigns.models import Campaign
    from apps.campaigns.services import campaign_audience

    reachable = Client.objects.create(
        first_name="A", phone="+996700000101", telegram_chat_id=111, marketing_consent=True
    )
    Client.objects.create(  # consent but no chat_id -> unreachable
        first_name="B", phone="+996700000102", marketing_consent=True
    )
    Client.objects.create(  # chat_id but no consent -> excluded
        first_name="C", phone="+996700000103", telegram_chat_id=333, marketing_consent=False
    )
    campaign = Campaign.objects.create(name="New arrivals", text_ru="👗")
    assert campaign_audience(campaign) == [reachable]


def test_campaign_audience_filters_bought_before(variant):
    from apps.campaigns.models import Campaign
    from apps.campaigns.services import campaign_audience

    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    buyer = Client.objects.create(
        first_name="Buyer", phone="+996700000201", telegram_chat_id=201, marketing_consent=True
    )
    Client.objects.create(
        first_name="Never", phone="+996700000202", telegram_chat_id=202, marketing_consent=True
    )
    order = SaleOrder.objects.create(client=buyer)
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)

    campaign = Campaign.objects.create(name="VIP", text_ru="hi", only_bought_before=True)
    assert campaign_audience(campaign) == [buyer]


def test_subscribe_and_unsubscribe_telegram():
    from apps.clients.services import subscribe_telegram, unsubscribe_telegram

    c = Client.objects.create(first_name="Sub", phone="+996 700 55-66-77")
    # Phone match is digit-based, tolerant of spaces/dashes/+.
    linked = subscribe_telegram("996700556677", 909)
    assert linked == c
    c.refresh_from_db()
    assert c.telegram_chat_id == 909 and c.marketing_consent is True

    unsubscribe_telegram(909)
    c.refresh_from_db()
    assert c.marketing_consent is False and c.telegram_chat_id == 909  # kept

    assert subscribe_telegram("000000", 1) is None  # no match


def test_send_campaign_marks_recipients_and_never_double_sends(monkeypatch, settings):
    from apps.campaigns.models import Campaign, CampaignRecipient

    settings.TELEGRAM_CLIENT_TOKEN = "test-token"
    calls = []

    def fake_send(token, chat_id, text, photos):
        calls.append(chat_id)
        return True, None, ""

    monkeypatch.setattr(
        "apps.campaigns.management.commands.send_campaign._telegram_send", fake_send
    )
    monkeypatch.setattr(
        "apps.campaigns.management.commands.send_campaign.time.sleep", lambda s: None
    )

    c = Client.objects.create(
        first_name="R", phone="+996700000301", telegram_chat_id=301, marketing_consent=True
    )
    campaign = Campaign.objects.create(name="Promo", text_ru="hello")

    call_command("send_campaign", campaign.pk)
    r = CampaignRecipient.objects.get(campaign=campaign, client=c)
    assert r.status == CampaignRecipient.SENT and r.sent_at is not None
    assert c.interactions.filter(kind=Interaction.MESSAGE).count() == 1
    assert calls == [301]

    # Re-running must not message the same client again.
    call_command("send_campaign", campaign.pk)
    assert calls == [301]  # unchanged
    assert CampaignRecipient.objects.filter(campaign=campaign).count() == 1


def test_lapsed_clients_only_returns_stale_buyers(variant):
    from apps.clients.services import lapsed_clients

    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    old_buyer = Client.objects.create(first_name="Old", phone="+996700000401")
    recent_buyer = Client.objects.create(first_name="New", phone="+996700000402")
    Client.objects.create(first_name="Never", phone="+996700000403")  # no purchase

    for cust, when in [
        (old_buyer, timezone.now() - timezone.timedelta(days=90)),
        (recent_buyer, timezone.now() - timezone.timedelta(days=5)),
    ]:
        o = SaleOrder.objects.create(client=cust)
        SaleItem.objects.create(order=o, variant=variant, quantity=1, unit_price=Decimal("3200"))
        confirm_sale(o)
        SaleOrder.objects.filter(pk=o.pk).update(confirmed_at=when)

    result = lapsed_clients(60)
    assert old_buyer in result
    assert recent_buyer not in result  # bought recently
    assert Client.objects.get(phone="+996700000403") not in result  # never bought


def test_confirmed_sale_som_value_is_frozen_against_later_rate_changes(variant, settings):
    # THE point of Part 0: a RUB sale's сом value is snapshotted at confirm time
    # and must NOT move when the owner later changes today's RUB rate.
    from django.core.cache import cache

    from apps.core.models import ExchangeRate

    cache.clear()
    settings.CURRENCY = "KGS"
    ExchangeRate.objects.create(currency="RUB", date=timezone.localdate(), rate=Decimal("1.10"))
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    order = SaleOrder.objects.create(currency="RUB")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order)
    order.refresh_from_db()
    assert order.rate_to_kgs == Decimal("1.100000")
    assert order.total_kgs == Decimal("1100.00")  # 1000 RUB × 1.10

    # Owner changes today's RUB rate afterwards.
    ExchangeRate.objects.filter(currency="RUB").update(rate=Decimal("2.00"))
    cache.clear()  # a real override clears this via signal; bulk update bypassed it

    order.refresh_from_db()
    assert order.total_kgs == Decimal("1100.00")  # UNCHANGED — historical value is frozen
    from django.db.models import Sum

    agg = SaleOrder.objects.filter(status=SaleOrder.CONFIRMED).aggregate(s=Sum("total_kgs"))["s"]
    assert agg == Decimal("1100.00")  # aggregates read the frozen field, don't re-convert


def test_payment_freezes_its_rate_and_void_cancels_exactly(variant, settings):
    from apps.core.models import ExchangeRate

    settings.CURRENCY = "KGS"
    ExchangeRate.objects.create(currency="RUB", date=timezone.localdate(), rate=Decimal("1.50"))
    cust = Client.objects.create(first_name="R", phone="+996700000700")
    p = Payment.objects.create(client=cust, amount=Decimal("200"), currency="RUB")
    assert p.rate_to_kgs == Decimal("1.500000")  # snapshotted on save
    assert p.amount_kgs == Decimal("300.00")  # 200 × 1.50
    reversal = void_payment(p)
    assert reversal.rate_to_kgs == p.rate_to_kgs  # same frozen rate
    assert reversal.amount_kgs == Decimal("-300.00")  # cancels the original exactly


def test_db_constraints_reject_bad_data_even_via_bulk_create(variant):
    from django.db import IntegrityError, transaction

    # clean() is bypassed by bulk_create — the DB CheckConstraints are not.
    with pytest.raises(IntegrityError), transaction.atomic():
        StockMovement.objects.bulk_create(
            [StockMovement(variant=variant, movement_type="adjustment", quantity=0, reason="x")]
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        StockMovement.objects.bulk_create(
            [StockMovement(variant=variant, movement_type="production_in", quantity=-5)]
        )
    c = Client.objects.create(first_name="A", phone="+996700000901")
    with pytest.raises(IntegrityError), transaction.atomic():
        Payment.objects.bulk_create([Payment(client=c, amount=Decimal("-10"))])
    order = SaleOrder.objects.create()
    with pytest.raises(IntegrityError), transaction.atomic():
        SaleItem.objects.bulk_create(
            [SaleItem(order=order, variant=variant, quantity=0, unit_price=Decimal("1"))]
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        SaleOrder.objects.create(total=Decimal("-1"))


def test_db_constraint_allows_negative_reversal(variant):
    # The one legitimate negative payment: a reversal linked to what it voids.
    c = Client.objects.create(first_name="B", phone="+996700000902")
    p = Payment.objects.create(client=c, amount=Decimal("10"))
    reversal = Payment.objects.create(client=c, amount=Decimal("-10"), reversed_payment=p)
    assert reversal.pk is not None


def test_argon2_is_default_and_pbkdf2_upgrades_on_login(client, django_user_model, settings):
    from django.contrib.auth.hashers import make_password

    # Argon2 must be the first (default) hasher.
    assert "Argon2" in settings.PASSWORD_HASHERS[0]

    # A user whose stored hash is the old PBKDF2 scheme...
    user = django_user_model.objects.create(username="legacy", is_staff=True)
    user.password = make_password("correct-horse-battery", hasher="pbkdf2_sha256")
    user.save(update_fields=["password"])
    assert user.password.startswith("pbkdf2_sha256$")

    # ...logs in through the real login view (axes needs a request) and gets
    # transparently re-hashed to Argon2 by Django's authenticate() path.
    token = client.get("/login/").context["csrf_token"]
    resp = client.post(
        "/login/",
        {"username": "legacy", "password": "correct-horse-battery", "csrfmiddlewaretoken": token},
    )
    assert resp.status_code == 302  # logged in, redirected
    user.refresh_from_db()
    assert user.password.startswith("argon2$"), "PBKDF2 hash should upgrade to Argon2 on login"


def test_csp_header_is_strict_on_pos_and_absent_on_admin(client, django_user_model):
    resp = client.get("/login/")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp.split("style-src")[0]  # no inline-script exception
    # Admin (Jazzmin, third-party inline scripts) is excluded from CSP.
    admin_resp = client.get("/panel/", follow=False)
    assert "Content-Security-Policy" not in admin_resp.headers


def _seed_grid(n, django_user_model):
    from apps.inventory.services import add_movement

    call_command("setup_roles")
    editor = django_user_model.objects.create_user(f"gridder{n}", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    cat = Category.objects.create(name=f"Grid{n}")
    for i in range(n):
        p = Product.objects.create(category=cat, name=f"P{n}-{i:04d}")
        v = ProductVariant.objects.create(
            product=p, sku=f"SKU{n}-{i:04d}", cost_price=Decimal("1"), sale_price=Decimal("100")
        )
        add_movement(v, StockMovement.PRODUCTION_IN, 5)
    return editor


def _grid_query_count(client, django_user_model, n):
    from django.core.cache import cache
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    editor = _seed_grid(n, django_user_model)
    client.force_login(editor)
    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    cache.clear()  # measure the cache-MISS path — the real query cost
    with CaptureQueriesContext(connection) as ctx:
        client.get(f"/pos/sale/{order_id}/products/")
    # Ignore session/auth/savepoint noise — count only catalog reads.
    catalog = [
        q
        for q in ctx.captured_queries
        if any(t in q["sql"] for t in ("inventory_product", "inventory_stockmovement"))
    ]
    return len(catalog)


def test_product_grid_query_budget_does_not_scale_with_rows(client, django_user_model):
    # ≤4 catalog queries at 10 products AND at 500 — proving the grid's query
    # count is constant, not O(rows). (The old per-product variant fetch was N+1.)
    assert _grid_query_count(client, django_user_model, 10) <= 4
    assert _grid_query_count(client, django_user_model, 500) <= 4


def test_stale_grid_cache_never_causes_an_oversell(client, django_user_model, variant):
    # Warm the grid cache with stock=1, then drain stock to 0 behind the cache
    # WITHOUT bumping the version, then confirm — the sale must still fail at
    # confirm time (fresh DB read under lock), never trust the cached badge.
    from django.core.cache import cache

    from apps.inventory.services import add_movement

    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 1)
    editor = django_user_model.objects.create_user("staler", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)
    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    client.get(f"/pos/sale/{order_id}/products/")  # warms grid cache (stock=1)
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})

    # Drain the stock directly, then FORCE the catalog version back so the grid
    # cache stays stale (simulating a read-from-stale-cache window).
    stale_version = cache.get("catalog:v")
    add_movement(variant, StockMovement.WRITEOFF_OUT, -1)  # stock now 0
    cache.set("catalog:v", stale_version)  # pin version stale

    resp = client.post(
        f"/pos/sale/{order_id}/confirm/", {"amount": "0", "currency": "KGS", "method": "cash"}
    )
    assert resp.status_code == 302
    assert resp.url == f"/pos/sale/{order_id}/"  # bounced with an error, not confirmed
    order = SaleOrder.objects.get(pk=order_id)
    assert order.status == SaleOrder.DRAFT  # never oversold


def test_client_list_query_budget(client, django_user_model):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    call_command("setup_roles")
    editor = django_user_model.objects.create_user("clister", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    for i in range(50):
        Client.objects.create(first_name=f"C{i}", phone=f"+99670000{i:04d}")
    client.force_login(editor)
    with CaptureQueriesContext(connection) as ctx:
        client.get("/pos/clients/?q=C")
    client_queries = [q for q in ctx.captured_queries if "clients_client" in q["sql"]]
    assert len(client_queries) <= 3


def test_confirm_query_budget(client, django_user_model, variant):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    editor = django_user_model.objects.create_user("confirmer", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)
    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 2})

    with CaptureQueriesContext(connection) as ctx:
        client.post(
            f"/pos/sale/{order_id}/confirm/",
            {"amount": "0", "currency": "KGS", "method": "cash"},
        )
    # Writes to the ledger, order, and history all count; keep it lean.
    writes_and_reads = [
        q for q in ctx.captured_queries if any(t in q["sql"] for t in ("sales_", "inventory_"))
    ]
    assert len(writes_and_reads) <= 10


def test_500_handler_renders_without_db_and_shows_correlation_id(rf, django_assert_num_queries):
    from apps.core.errors import server_error

    request = rf.get("/pos/")
    request.correlation_id = "abc123def456"
    # The DB may be what's down — the 500 page must touch it zero times.
    with django_assert_num_queries(0):
        resp = server_error(request)
    assert resp.status_code == 500
    body = resp.content.decode()
    assert "abc123def456" in body  # the id, to correlate with the server log
    assert "Что-то сломалось" in body
    assert "/pos/" in body
    # No leakage of internals.
    assert "Traceback" not in body and "Django" not in body and "Sorry" not in body


def test_404_page_is_russian(client, settings):
    settings.DEBUG = False  # custom handlers only fire when DEBUG is off
    resp = client.get("/definitely-not-a-real-page/")
    assert resp.status_code == 404
    assert "Страница не найдена" in resp.content.decode()
    assert "Traceback" not in resp.content.decode()


def test_403_page_on_role_violation(client, django_user_model, settings):
    settings.DEBUG = False
    call_command("setup_roles")
    viewer = django_user_model.objects.create_user("viewer_err", password="x" * 12, is_staff=True)
    viewer.groups.add(Group.objects.get(name=VIEWER))
    client.force_login(viewer)
    resp = client.get("/pos/sale/1/")  # Viewer has no sales.add_saleorder -> 403
    assert resp.status_code == 403
    assert "Нет доступа" in resp.content.decode()


def test_csrf_failure_page_is_russian(settings):
    from django.test import Client

    settings.DEBUG = False
    c = Client(enforce_csrf_checks=True)
    resp = c.post("/login/", {"username": "x", "password": "y"})  # no CSRF token
    assert resp.status_code == 403
    assert "Сессия устарела" in resp.content.decode()


def test_healthz_ok_when_db_and_cache_up(client):
    resp = client.get("/healthz/")
    assert resp.status_code == 200
    assert resp.content == b"ok"


def test_healthz_503_when_cache_roundtrip_fails(client, monkeypatch):
    # Simulate Redis down: set is a no-op and get returns None (exactly how
    # django-redis behaves with IGNORE_EXCEPTIONS). The round-trip check must
    # then report the cache as unhealthy with a 503.
    from django.core import cache as cache_module

    monkeypatch.setattr(cache_module.cache, "set", lambda *a, **k: False)
    monkeypatch.setattr(cache_module.cache, "get", lambda *a, **k: None)
    resp = client.get("/healthz/")
    assert resp.status_code == 503
    assert b"cache" in resp.content


def _wa_sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _wa_text_payload(phone, text):
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {"from": phone, "type": "text", "text": {"body": text}}
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()


def test_wa_webhook_valid_signature_passes_and_links_client(client, settings):
    settings.WHATSAPP_APP_SECRET = "topsecret"
    body = _wa_text_payload("996700123456", "stock")
    resp = client.post(
        "/wa/webhook/",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=_wa_sig("topsecret", body),
    )
    assert resp.status_code == 200
    assert Client.objects.filter(phone="996700123456").exists()  # CRM-linked


def test_wa_webhook_tampered_body_rejected(client, settings):
    settings.WHATSAPP_APP_SECRET = "topsecret"
    body = _wa_text_payload("996700123456", "stock")
    sig = _wa_sig("topsecret", body)  # signature for the ORIGINAL body
    resp = client.post(
        "/wa/webhook/",
        data=body + b" ",  # one byte of tampering
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=sig,
    )
    assert resp.status_code == 403
    assert not Client.objects.filter(phone="996700123456").exists()


def test_wa_webhook_missing_signature_rejected(client, settings):
    settings.WHATSAPP_APP_SECRET = "topsecret"
    resp = client.post("/wa/webhook/", data=b"{}", content_type="application/json")
    assert resp.status_code == 403


def test_wa_webhook_rejects_when_secret_unset(client, settings):
    settings.WHATSAPP_APP_SECRET = ""  # fail closed, never process unsigned
    body = _wa_text_payload("996700123456", "stock")
    resp = client.post(
        "/wa/webhook/",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=_wa_sig("anything", body),
    )
    assert resp.status_code == 403


def test_wa_webhook_oversize_payload_rejected(client, settings):
    settings.WHATSAPP_APP_SECRET = "topsecret"
    big = b'{"x":"' + b"a" * (200 * 1024) + b'"}'
    resp = client.post(
        "/wa/webhook/",
        data=big,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256="sha256=00",
    )
    assert resp.status_code == 413  # rejected before the HMAC even runs


def test_wa_webhook_rate_limited_per_ip(client, settings, monkeypatch):
    from django.core.cache import cache

    cache.clear()
    settings.WHATSAPP_APP_SECRET = "topsecret"
    monkeypatch.setattr("apps.wa.views.RATE_LIMIT", 2)
    # First 2 are allowed through (then 403 on the missing signature); the 3rd
    # trips the limit and short-circuits with 429 before any signature work.
    for _ in range(2):
        client.post("/wa/webhook/", data=b"{}", content_type="application/json")
    resp = client.post("/wa/webhook/", data=b"{}", content_type="application/json")
    assert resp.status_code == 429


def test_wa_webhook_rate_limit_uses_real_ip_not_forged_xff(client, settings, monkeypatch):
    from django.core.cache import cache

    cache.clear()
    settings.WHATSAPP_APP_SECRET = "topsecret"
    monkeypatch.setattr("apps.wa.views.RATE_LIMIT", 1)
    # An attacker rotates X-Forwarded-For to dodge the limit, but Caddy sets
    # X-Real-IP to the true peer. The limiter must key on X-Real-IP, so both
    # requests land in the same bucket and the 2nd is blocked.
    client.post(
        "/wa/webhook/",
        data=b"{}",
        content_type="application/json",
        HTTP_X_REAL_IP="203.0.113.7",
        HTTP_X_FORWARDED_FOR="9.9.9.9",
    )
    resp = client.post(
        "/wa/webhook/",
        data=b"{}",
        content_type="application/json",
        HTTP_X_REAL_IP="203.0.113.7",
        HTTP_X_FORWARDED_FOR="8.8.8.8",  # forged, rotated — must NOT create a new bucket
    )
    assert resp.status_code == 429


def _freeze_now(monkeypatch, y, mo, d, h, mi):
    """Pin timezone.now() to a fixed UTC instant so localdate() is deterministic."""
    import datetime

    fixed = datetime.datetime(y, mo, d, h, mi, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr("django.utils.timezone.now", lambda: fixed)


def _sale_confirmed_at(variant, y, mo, d, h, mi):
    """A confirmed sale whose confirmed_at is pinned to a UTC instant."""
    import datetime

    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    order = SaleOrder.objects.create()
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)
    SaleOrder.objects.filter(pk=order.pk).update(
        confirmed_at=datetime.datetime(y, mo, d, h, mi, tzinfo=datetime.timezone.utc)
    )
    return order


# Force Asia/Bishkek (UTC+6) as the active timezone so these tests are correct
# regardless of the ambient TIME_ZONE, and both localdate() and the ORM __date
# lookup agree on which day a sale belongs to.
def test_sale_at_2330_bishkek_counts_in_that_local_day(variant, monkeypatch):
    # 23:30 Asia/Bishkek on 16 Jul == 17:30 UTC on 16 Jul.
    _sale_confirmed_at(variant, 2026, 7, 16, 17, 30)
    from apps.sales.services import today_summary

    with timezone.override("Asia/Bishkek"):
        # "Now" is 23:45 Bishkek on the 16th — the sale is in TODAY's numbers.
        _freeze_now(monkeypatch, 2026, 7, 16, 17, 45)
        assert today_summary()["orders"] == 1

        # 15 minutes later it's 00:15 Bishkek on the 17th — the SAME sale must NOT
        # leak into the next day's numbers (that's the silent bug we're guarding).
        _freeze_now(monkeypatch, 2026, 7, 16, 18, 15)
        assert today_summary()["orders"] == 0


def test_early_morning_sale_counts_in_local_day_not_utc_day(variant, monkeypatch):
    # The real UTC-boundary case: 00:30 Bishkek on 17 Jul == 18:30 UTC on 16 Jul,
    # so the UTC date (16th) differs from the local date (17th). A UTC-based
    # "today" would drop this sale from the 17th's report — the corruption.
    order = _sale_confirmed_at(variant, 2026, 7, 16, 18, 30)
    from apps.sales.services import today_summary, todays_confirmed_orders

    with timezone.override("Asia/Bishkek"):
        _freeze_now(monkeypatch, 2026, 7, 17, 4, 0)  # 10:00 Bishkek on the 17th
        assert today_summary()["orders"] == 1  # /pos «Сегодня» + bot /today
        assert order.pk in [o.pk for o in todays_confirmed_orders()]  # daily report Продажи


def test_pos_segodnya_view_shows_boundary_sale_in_local_day(
    client, django_user_model, variant, monkeypatch
):
    call_command("setup_roles")
    editor = django_user_model.objects.create_user("tzeditor", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)
    _sale_confirmed_at(variant, 2026, 7, 16, 18, 30)  # 00:30 Bishkek 17th
    with timezone.override("Asia/Bishkek"):
        _freeze_now(monkeypatch, 2026, 7, 17, 4, 0)  # 10:00 Bishkek 17th
        resp = client.get("/pos/today/")
    assert resp.status_code == 200
    assert "3200" in resp.content.decode()  # the boundary sale is on TODAY's screen


def test_bot_today_reply_counts_boundary_sale_in_local_day(variant, monkeypatch):
    _sale_confirmed_at(variant, 2026, 7, 16, 18, 30)  # 00:30 Bishkek 17th
    from apps.wa.replies import today_reply

    with timezone.override("Asia/Bishkek"):
        _freeze_now(monkeypatch, 2026, 7, 17, 4, 0)  # 10:00 Bishkek 17th
        assert "1 sales" in today_reply()  # bot /today, via today_summary


def test_catalog_version_bump_survives_absent_key():
    from django.core.cache import cache

    from apps.inventory.cache import CATALOG_VERSION_KEY, bump_catalog_version, catalog_version

    # Simulate Redis having dropped the key (restart / eviction / expiry).
    cache.delete(CATALOG_VERSION_KEY)
    bump_catalog_version()  # must NOT raise ValueError
    assert isinstance(catalog_version(), int)

    # And a product save (which fires the post_save signal -> bump) must not blow
    # up either when the version key is missing.
    cache.delete(CATALOG_VERSION_KEY)
    cat = Category.objects.create(name="Cache-Cat")
    Product.objects.create(category=cat, name="Cache-Prod")  # signal path, no raise
    assert catalog_version() >= 1


def test_cleanup_draft_sales_deletes_only_old_drafts():
    fresh = SaleOrder.objects.create()
    old = SaleOrder.objects.create()
    SaleOrder.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - timezone.timedelta(hours=30)
    )
    call_command("cleanup_draft_sales")
    assert SaleOrder.objects.filter(pk=fresh.pk).exists()
    assert not SaleOrder.objects.filter(pk=old.pk).exists()


# =========================================================================
# Owner dashboard (Phase 3) — access control, HTMX, query budget, caching.
# =========================================================================


def test_dashboard_is_owner_only(client, django_user_model):
    call_command("setup_roles")
    # Anonymous -> bounced to the shared login, never rendered.
    assert client.get("/dashboard/").status_code == 302

    # Editor (staff, no superuser) -> 403, and never sees the surface.
    editor = django_user_model.objects.create_user("dasheditor", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)
    assert client.get("/dashboard/").status_code == 403

    # Owner (superuser) -> 200 with the real content.
    client.force_login(django_user_model.objects.create_superuser("dashowner", password="x" * 12))
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert "Выручка" in resp.content.decode()


def test_dashboard_link_shown_only_to_owner(client, django_user_model):
    call_command("setup_roles")
    editor = django_user_model.objects.create_user("linkeditor", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)
    assert 'href="/dashboard/"' not in client.get("/pos/", follow=True).content.decode()

    client.force_login(django_user_model.objects.create_superuser("linkowner", password="x" * 12))
    assert 'href="/dashboard/"' in client.get("/pos/", follow=True).content.decode()


def test_dashboard_htmx_swaps_only_the_panel(client, django_user_model):
    call_command("setup_roles")
    client.force_login(django_user_model.objects.create_superuser("dashhx", password="x" * 12))
    full = client.get("/dashboard/?period=today")
    assert b"<html" in full.content  # a normal hit renders the shell
    partial = client.get("/dashboard/?period=today", HTTP_HX_REQUEST="true")
    assert b"<html" not in partial.content  # HTMX gets only the panel fragment
    assert "Выручка" in partial.content.decode()


def test_dashboard_bad_period_falls_back_to_default(client, django_user_model):
    call_command("setup_roles")
    client.force_login(django_user_model.objects.create_superuser("dashbad", password="x" * 12))
    resp = client.get("/dashboard/?period=not-a-real-period")
    assert resp.status_code == 200  # resolve_period() coerces it, never 500s


def _dash_catalog():
    """10 variants + 20 clients to spread the seeded sales across."""
    cat = Category.objects.create(name="Dash")
    variants = [
        ProductVariant.objects.create(
            product=Product.objects.create(category=cat, name=f"DP{i}"),
            sku=f"DSKU{i}",
            cost_price=Decimal("400"),
            sale_price=Decimal("1000"),
        )
        for i in range(10)
    ]
    clients = [
        Client.objects.create(first_name=f"DC{i}", phone=f"+99655500{i:04d}") for i in range(20)
    ]
    return variants, clients


def _seed_dashboard_sales(n, variants, clients, day_offset=0):
    """Bulk-create n confirmed KGS sales spread across the last year, each with a
    line item and (for ~70%) a full payment — so every panel has data. Bulk, not
    confirm_sale(), so 5000 rows seed in one shot; PKs come back from bulk_create."""
    now = timezone.now()
    orders = [
        SaleOrder(
            status=SaleOrder.CONFIRMED,
            channel=SaleOrder.SHOP if i % 2 else SaleOrder.INSTAGRAM,
            client=clients[i % len(clients)],
            total=Decimal("1000"),
            currency="KGS",
            rate_to_kgs=Decimal("1"),
            total_kgs=Decimal("1000"),
            confirmed_at=now - timezone.timedelta(days=(day_offset + i) % 360, hours=i % 24),
        )
        for i in range(n)
    ]
    created = SaleOrder.objects.bulk_create(orders, batch_size=1000)
    items, pays = [], []
    for idx, o in enumerate(created):
        items.append(
            SaleItem(order_id=o.pk, variant=variants[idx % len(variants)], quantity=1, unit_price=Decimal("1000"))
        )
        if idx % 10 < 7:  # leave ~30% as debt so the Долги panel is exercised
            pays.append(
                Payment(
                    order_id=o.pk,
                    client=clients[idx % len(clients)],
                    amount=Decimal("1000"),
                    currency="KGS",
                    rate_to_kgs=Decimal("1"),
                    method=Payment.CASH,
                )
            )
    SaleItem.objects.bulk_create(items, batch_size=1000)
    Payment.objects.bulk_create(pays, batch_size=1000)


def test_dashboard_query_budget_is_flat_and_bounded(django_user_model):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.reports.dashboard import PERIODS, dashboard_data

    def count(period):
        with CaptureQueriesContext(connection) as ctx:
            dashboard_data(period)
        return len(ctx.captured_queries)

    variants, clients = _dash_catalog()
    _seed_dashboard_sales(50, variants, clients)
    small = {p: count(p) for p in PERIODS}
    _seed_dashboard_sales(4950, variants, clients, day_offset=7)  # 5000 total
    big = {p: count(p) for p in PERIODS}

    for p in PERIODS:
        assert big[p] <= 12, f"{p}: {big[p]} queries at 5000 sales (budget 12)"
        assert big[p] == small[p], f"{p} query count scaled with rows: {small[p]} -> {big[p]}"


def test_dead_stock_does_not_n_plus_one(django_user_model):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.inventory.services import add_movement
    from apps.reports.dashboard import _dead_stock

    cat = Category.objects.create(name="Dead")
    old = timezone.now() - timezone.timedelta(days=200)

    def make(k, tag):
        for i in range(k):
            p = Product.objects.create(category=cat, name=f"DS{tag}{i}")
            Product.objects.filter(pk=p.pk).update(created_at=old)  # 200 days idle -> dead
            v = ProductVariant.objects.create(
                product=p, sku=f"DSK{tag}{i}", cost_price=Decimal("100"), sale_price=Decimal("200")
            )
            add_movement(v, StockMovement.PRODUCTION_IN, 5)  # in stock

    make(5, "A")
    with CaptureQueriesContext(connection) as few:
        result_few = _dead_stock()
    make(200, "B")
    with CaptureQueriesContext(connection) as many:
        result_many = _dead_stock()

    assert result_few and result_many  # the loop actually ran over dead rows
    # Two queries (stock subquery + one grouped last-sale), constant as rows grow.
    assert len(many.captured_queries) == len(few.captured_queries) <= 2


def test_dashboard_cache_invalidated_on_sale_confirm(variant):
    from apps.reports.views import _cached_data

    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    before = _cached_data("today")["metrics"]["revenue"]["value"]  # warms the cache

    order = SaleOrder.objects.create()
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)  # bumps catalog_version via the StockMovement signal

    after = _cached_data("today")["metrics"]["revenue"]["value"]
    # A stale cache would return `before`; the version-keyed cache must refresh.
    assert after == before + Decimal("3200")


def test_dashboard_cache_invalidated_on_payment(variant):
    from apps.reports.views import _cached_data

    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    debtor = Client.objects.create(first_name="Debtor", phone="+996700009999")
    order = SaleOrder.objects.create(client=debtor)
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)  # unpaid -> a debt
    debt_before = _cached_data("today")["metrics"]["debt"]["value"]

    record_payment(order, amount=Decimal("1200"), currency="KGS", method="cash")  # Payment signal bumps
    debt_after = _cached_data("today")["metrics"]["debt"]["value"]
    assert debt_after == debt_before - Decimal("1200")
