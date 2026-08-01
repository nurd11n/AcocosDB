import hashlib
import hmac
import json
import re
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
from apps.clients.services import (
    client_credits,
    client_debt,
    debtors_report_rows,
    log_whatsapp_interaction,
)
from apps.core.currency import from_base, get_rate, to_base
from apps.core.management.commands.send_daily_report import (
    _debts_rows,
    _sales_rows,
    _stock_rows,
    _unreviewed_rows,
)
from apps.core.models import ExchangeRate, RateChangeLog
from apps.core.permissions import EDITOR, VIEWER
from apps.inventory.models import Category, Product, ProductVariant, StockMovement
from apps.inventory.services import add_movement, adjust_to_count
from apps.reports.models import DailyReview
from apps.sales.models import Payment, SaleItem, SaleOrder
from apps.sales.services import (
    balance_kgs_before_payment,
    cancel_sale,
    compute_change_preview,
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


def test_variant_stock_panel_receive_writeoff_and_recount(client, django_user_model, variant):
    """The per-variant stock panel on the change page: +Принять, −Списать, and
    =Пересчёт each write a ledger row through services and update the remainder,
    reachable in one click, no bulk action, no raw movement form."""
    from django.urls import reverse

    owner = django_user_model.objects.create_superuser("stockowner", password="x" * 12)
    client.force_login(owner)
    move_url = reverse("admin:inventory_productvariant_stockmove", args=[variant.pk])

    # Change page renders the panel with the live remainder + history.
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    page = client.get(reverse("admin:inventory_productvariant_change", args=[variant.pk]))
    assert page.status_code == 200
    assert "Остаток на складе" in page.content.decode()

    # + Принять 10 -> 15
    client.post(move_url, {"op": "receive", "qty": "10"})
    assert variant.stock == 15
    # − Списать 3 -> 12
    client.post(move_url, {"op": "writeoff", "qty": "3"})
    assert variant.stock == 12
    # = Пересчёт to exactly 8 (with reason) -> 8, via an ADJUSTMENT row
    client.post(move_url, {"op": "recount", "count": "8", "reason": "инвентаризация"})
    assert variant.stock == 8

    # Every change is a ledger row — the audit trail is intact, not overwritten.
    kinds = list(variant.movements.values_list("movement_type", flat=True))
    assert StockMovement.ADJUSTMENT in kinds and StockMovement.WRITEOFF_OUT in kinds


def test_variant_stock_panel_rejects_overdraw_and_missing_reason(
    client, django_user_model, variant
):
    from django.urls import reverse

    client.force_login(django_user_model.objects.create_superuser("stockowner2", password="x" * 12))
    move_url = reverse("admin:inventory_productvariant_stockmove", args=[variant.pk])
    add_movement(variant, StockMovement.PRODUCTION_IN, 4)

    client.post(move_url, {"op": "writeoff", "qty": "9"})  # more than in stock
    assert variant.stock == 4  # unchanged
    client.post(move_url, {"op": "recount", "count": "2", "reason": ""})  # no reason
    assert variant.stock == 4  # unchanged


def test_variant_stock_panel_blocked_for_viewer(client, django_user_model, variant):
    from django.urls import reverse

    from apps.core.permissions import VIEWER

    call_command("setup_roles")
    viewer = django_user_model.objects.create_user("viewer_s", password="x" * 12, is_staff=True)
    viewer.groups.add(Group.objects.get(name=VIEWER))
    client.force_login(viewer)
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    move_url = reverse("admin:inventory_productvariant_stockmove", args=[variant.pk])
    resp = client.post(move_url, {"op": "receive", "qty": "10"})
    assert resp.status_code in (403, 302)  # no change permission
    assert variant.stock == 5  # unchanged


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
    assert any(row[0] == "Aiperi" and row[2] == "5400.00" and row[3] == "KGS" for row in debts[1:])


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


def test_foreign_currency_payment_converts_and_counts_toward_balance(variant):
    """A USD payment on a сом order is converted at the NBKR rate frozen onto
    the payment and reduces the сом balance + the client's сом debt. (The owner
    opted into conversion; the POS shows a 'verify the rate' note.)"""
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87"))
    c = Client.objects.create(first_name="Aliya", phone="+996700000012")
    order = SaleOrder.objects.create(client=c, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("8700"))
    confirm_sale(order)  # total 8700 сом
    # 40 USD × 87 = 3480 сом toward the 8700 сом order.
    Payment.objects.create(client=c, order=order, amount=Decimal("40"), currency="USD")
    assert order.paid_amount == Decimal("3480.00")
    assert order.balance == Decimal("5220.00")
    assert order.payment_status == SaleOrder.PARTIAL
    assert client_debt(c) == {"KGS": Decimal("5220.00")}


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


def test_currency_conversion_uses_current_rate(settings):
    settings.CURRENCY = "KGS"
    # One row per currency — the current rate. The date arg to the helpers is
    # accepted for compatibility but ignored (rates are current-only now).
    ExchangeRate.objects.create(currency="USD", date=date(2026, 7, 12), rate=Decimal("90"))
    any_day = date(2026, 7, 13)

    # Base currency is always 1:1, no rate needed, in either direction.
    assert to_base(Decimal("9000"), "KGS", any_day) == Decimal("9000.00")
    assert from_base(Decimal("9000"), "KGS", any_day) == Decimal("9000.00")

    assert get_rate("USD") == Decimal("90")
    # from_base: KGS -> USD divides by the rate; to_base multiplies.
    assert from_base(Decimal("9000"), "USD", any_day) == Decimal("100.00")
    assert to_base(Decimal("100"), "USD", any_day) == Decimal("9000.00")

    # A currency with no rate on record -> None (caller falls back to base).
    assert from_base(Decimal("9000"), "RUB", any_day) is None
    assert to_base(Decimal("100"), "RUB", any_day) is None


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


def test_fetch_rates_overwrites_existing_row_in_place(monkeypatch, settings):
    settings.CURRENCY = "KGS"
    # An old rate for USD; a fetch overwrites it in place (one row per currency,
    # no dated pile-up).
    ExchangeRate.objects.create(
        currency="USD", date=date(2026, 7, 10), rate=Decimal("99.99"), source=ExchangeRate.MANUAL
    )
    monkeypatch.setattr(
        "apps.core.management.commands.fetch_rates.requests.get", lambda *a, **k: _FakeResp()
    )
    call_command("fetch_rates")
    assert ExchangeRate.objects.filter(currency="USD").count() == 1
    row = ExchangeRate.objects.get(currency="USD")
    assert row.rate == Decimal("87.45") and row.source == ExchangeRate.NBKR


def test_fetch_rates_survives_network_failure(monkeypatch, settings):
    settings.CURRENCY = "KGS"

    def boom(*a, **k):
        raise requests.RequestException("network down")

    monkeypatch.setattr("apps.core.management.commands.fetch_rates.requests.get", boom)
    call_command("fetch_rates")  # must not raise
    assert not ExchangeRate.objects.exists()  # nothing written, last known kept


def test_fetch_rates_routes_through_nbkr_proxy_when_set(monkeypatch, settings):
    """nbkr.kg blocks some server IPs — when NBKR_PROXY is set, the fetch (and
    only the fetch) goes through it; unset, the request is direct (proxies=None)."""
    settings.CURRENCY = "KGS"
    captured = {}

    def capture_get(url, **kwargs):
        captured["proxies"] = kwargs.get("proxies")
        return _FakeResp()

    monkeypatch.setattr("apps.core.management.commands.fetch_rates.requests.get", capture_get)

    settings.NBKR_PROXY = ""
    call_command("fetch_rates")
    assert captured["proxies"] is None  # direct

    settings.NBKR_PROXY = "socks5://10.0.0.9:1080"
    call_command("fetch_rates")
    assert captured["proxies"] == {
        "http": "socks5://10.0.0.9:1080",
        "https": "socks5://10.0.0.9:1080",
    }


def test_owner_can_set_rate_by_hand_from_the_pos_card(client, django_user_model, settings):
    """The «Изменить курс» dialog: an Owner hand-enters a rate (used where NBKR
    is unreachable), stored as a MANUAL override and logged like the /panel/
    ExchangeRate admin. Comma decimals are accepted; a blank field is left
    untouched."""
    from apps.core.models import ExchangeRate, RateChangeLog

    settings.CURRENCY = "KGS"
    owner = django_user_model.objects.create_superuser("rate_owner", "o@e.com", "x" * 12)
    client.force_login(owner)

    # The modal opens (Owner) and offers a USD field.
    resp = client.get("/pos/rates/edit/")
    assert resp.status_code == 200
    assert 'name="rate_USD"' in resp.content.decode()

    # Save USD (comma decimal), leave RUB blank.
    resp = client.post("/pos/rates/save/", {"rate_USD": "87,45", "rate_RUB": ""})
    assert resp.status_code == 200
    usd = ExchangeRate.objects.get(currency="USD")
    assert usd.rate == Decimal("87.45") and usd.source == ExchangeRate.MANUAL
    assert RateChangeLog.objects.filter(
        currency="USD", source=ExchangeRate.MANUAL, changed_by=owner
    ).exists()
    assert not ExchangeRate.objects.filter(currency="RUB").exists()  # blank = untouched
    assert 'id="rate-modal"' in resp.content.decode()  # dialog closes (oob)


def test_manual_rate_entry_is_owner_only(client, django_user_model, settings):
    from apps.core.models import ExchangeRate
    from apps.core.permissions import EDITOR

    call_command("setup_roles")
    editor = django_user_model.objects.create_user("rate_editor", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    assert client.get("/pos/rates/edit/").status_code == 403
    assert client.post("/pos/rates/save/", {"rate_USD": "90"}).status_code == 403
    assert not ExchangeRate.objects.exists()  # nothing written by a non-Owner


def test_rate_save_rejects_get(client, django_user_model):
    owner = django_user_model.objects.create_superuser("rate_owner2", "o@e.com", "x" * 12)
    client.force_login(owner)
    assert client.get("/pos/rates/save/").status_code == 405  # POST-only mutation


def test_convert_crosses_currencies_via_base():
    from apps.core.currency import convert

    d = date(2026, 7, 12)
    ExchangeRate.objects.create(currency="USD", date=d, rate=Decimal("90"))
    ExchangeRate.objects.create(currency="RUB", date=d, rate=Decimal("1.2"))
    # Same currency is a no-op (quantized).
    assert convert(Decimal("100"), "KGS", "KGS", d) == Decimal("100.00")
    # 40 USD -> KGS (× 90).
    assert convert(Decimal("40"), "USD", "KGS", d) == Decimal("3600.00")
    # 3600 KGS -> USD (÷ 90).
    assert convert(Decimal("3600"), "KGS", "USD", d) == Decimal("40.00")
    # USD -> RUB crosses via KGS: 40 × 90 ÷ 1.2 = 3000 RUB.
    assert convert(Decimal("40"), "USD", "RUB", d) == Decimal("3000.00")
    # No rate on record for a leg -> None (caller keeps it in its own ccy).
    ExchangeRate.objects.filter(currency="RUB").delete()
    assert convert(Decimal("40"), "USD", "RUB", d) is None


def test_foreign_payment_preview_converts_balance_and_shows_note(
    client, django_user_model, variant
):
    """The sale-screen preview converts a foreign payment into the order's
    currency, deducts it, and shows the 'verify the rate' note."""
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87"))
    editor = django_user_model.objects.create_user("editor_fx", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    order_id = int(client.get("/pos/").url.rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})
    # variant sells for 3200 сом; pay 10 USD (= 870 сом) -> remaining 2330 сом.
    resp = client.post(
        f"/pos/sale/{order_id}/recalc/", {"amount": "10", "currency": "USD", "method": "cash"}
    )
    body = resp.content.decode()
    assert "2\xa0330" in body  # converted balance, not the untouched 3200
    assert "НБКР" in body  # the verify-manually note is shown


def test_refresh_rates_button_pulls_fresh_and_rerenders(
    client, django_user_model, variant, monkeypatch, settings
):
    settings.CURRENCY = "KGS"
    call_command("setup_roles")
    editor = django_user_model.objects.create_user("editor_rt", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    monkeypatch.setattr(
        "apps.core.management.commands.fetch_rates.requests.get", lambda *a, **k: _FakeResp()
    )
    assert not ExchangeRate.objects.exists()
    resp = client.post("/pos/rates/refresh/")
    assert resp.status_code == 200
    # Pulled today's USD/RUB and rendered them into the strip.
    assert ExchangeRate.objects.filter(currency="USD", date=timezone.localdate()).exists()
    assert "87,45" in resp.content.decode()  # RU locale renders the decimal with a comma


def test_refresh_rates_survives_nbkr_failure(client, django_user_model, monkeypatch):
    call_command("setup_roles")
    editor = django_user_model.objects.create_user("editor_rf", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    def boom(*a, **k):
        raise requests.RequestException("nbkr down")

    monkeypatch.setattr("apps.core.management.commands.fetch_rates.requests.get", boom)
    resp = client.post("/pos/rates/refresh/")  # must not 500
    assert resp.status_code == 200


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


def test_voided_payment_excluded_from_method_mix(variant):
    # D1 regression: a voided payment must NOT colour the «Способы оплаты» donut.
    # Dropping only the negative reversal row would leave the +amount original
    # overstating its method; the fix drops the original it voided too.
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    c = Client.objects.create(first_name="Nargiza", phone="+996700000099")
    order = SaleOrder.objects.create(client=c)
    SaleItem.objects.create(order=order, variant=variant, quantity=2, unit_price=Decimal("3200"))
    confirm_sale(order)
    cash = Payment.objects.create(
        client=c, order=order, amount=Decimal("3200"), method=Payment.CASH
    )
    Payment.objects.create(client=c, order=order, amount=Decimal("3200"), method=Payment.MBANK)

    void_payment(cash)  # the cash payment was a mistake

    from apps.reports.dashboard import dashboard_data

    methods = dashboard_data("today")["methods"]
    total = sum((m["value"] for m in methods), Decimal("0"))
    # Only the surviving MBank payment remains; the voided cash is gone entirely
    # (both the −3200 reversal and the +3200 original), so the mix is one method.
    # The pre-fix bug would report two methods totalling 6400.
    assert len(methods) == 1
    assert total == Decimal("3200")


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
    """Part 1c: client-side/cart-time caps are UX, not the defence — stock can
    still change between the cart being built and confirm (a concurrent sale,
    a write-off). Add 5 while 5 are in stock (the cart-time cap at add-time
    allows it), THEN reduce stock to 2 before confirming — confirm must still
    fail cleanly and keep the basket, never a silent oversell."""
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    editor = django_user_model.objects.create_user("editor13", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 5})
    add_movement(variant, StockMovement.WRITEOFF_OUT, 3)  # stock 5 -> 2, AFTER adding to cart

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
    assert order.items.first().quantity == 5
    variant.refresh_from_db()
    assert variant.stock == 2  # nothing written off by the failed confirm itself


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
    from apps.clients.services import (
        set_marketing_consent,
        subscribe_telegram,
        unsubscribe_telegram,
    )

    c = Client.objects.create(first_name="Sub", phone="+996 700 55-66-77")
    # Phone match is digit-based, tolerant of spaces/dashes/+.
    linked = subscribe_telegram("996700556677", 909)
    assert linked == c
    c.refresh_from_db()
    # CLIENT_BOTS.md §3.1: sharing a phone makes the chat reachable but must
    # NOT itself set consent — that's a separate explicit Да/Нет step.
    assert c.telegram_chat_id == 909
    assert c.marketing_consent is False

    set_marketing_consent(c, True)
    c.refresh_from_db()
    assert c.marketing_consent is True

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

    call_command("send_campaign", campaign.pk, ignore_quiet_hours=True)
    r = CampaignRecipient.objects.get(campaign=campaign, client=c)
    assert r.status == CampaignRecipient.SENT and r.sent_at is not None
    assert c.interactions.filter(kind=Interaction.MESSAGE).count() == 1
    assert calls == [301]

    # Re-running must not message the same client again.
    call_command("send_campaign", campaign.pk, ignore_quiet_hours=True)
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


def test_healthz_503_when_db_is_down(client, monkeypatch):
    from django.db import connection

    def broken_cursor(*a, **k):
        raise OSError("could not connect to server")

    monkeypatch.setattr(connection, "cursor", broken_cursor)
    resp = client.get("/healthz/")
    assert resp.status_code == 503
    assert b"db" in resp.content


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


def test_wa_webhook_404s_when_whatsapp_disabled(client, settings):
    """Bots are not production-ready — WHATSAPP_ENABLED=False (the shipped
    prod default) must make the whole route unreachable, GET and POST alike,
    regardless of credentials being otherwise valid."""
    settings.WHATSAPP_ENABLED = False
    settings.WHATSAPP_APP_SECRET = "topsecret"
    settings.WHATSAPP_VERIFY_TOKEN = "verify-me"

    assert (
        client.get(
            "/wa/webhook/", {"hub.verify_token": "verify-me", "hub.challenge": "x"}
        ).status_code
        == 404
    )

    body = _wa_text_payload("996700123456", "stock")
    resp = client.post(
        "/wa/webhook/",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=_wa_sig("topsecret", body),
    )
    assert resp.status_code == 404
    assert not Client.objects.filter(phone="996700123456").exists()


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
    from apps.pos.templatetags.pos_extras import money_filter

    assert (
        money_filter(3200, "KGS") in resp.content.decode()
    )  # the boundary sale is on TODAY's screen


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


def test_dashboard_numeric_svg_attrs_are_not_locale_formatted(client, django_user_model, variant):
    # Regression: LANGUAGE_CODE="ru" makes Django render raw numbers with a
    # comma decimal separator ("104.0" -> "104,0") wherever a template renders
    # a float/int directly. That's invalid inside SVG coordinate attributes
    # and breaks the bar's --pct CSS custom property (a comma inside scaleX()
    # makes the whole `transform` declaration invalid, so the browser drops it
    # and the bar never scales). The panel must render with locale formatting
    # off regardless of what LANGUAGE_CODE the project ships.
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    c1 = Client.objects.create(first_name="Aigerim", phone="+996700000097")
    order = SaleOrder.objects.create(client=c1, channel=SaleOrder.SHOP)
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)
    # A second channel with a different amount so at least one bar's --pct
    # fraction is non-round (e.g. 0.3333...), the case most likely to leak a
    # locale comma through an untested happy path.
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    order2 = SaleOrder.objects.create(client=c1, channel=SaleOrder.WHOLESALE)
    SaleItem.objects.create(order=order2, variant=variant, quantity=1, unit_price=Decimal("1600"))
    confirm_sale(order2)

    client.force_login(django_user_model.objects.create_superuser("svgowner", password="x" * 12))
    html = client.get("/dashboard/?period=month").content.decode()

    # Scoped to the attributes that carry a RAW single float/int (x, y, cx, cy,
    # r, data-cx, data-cy) — never `d`, whose path strings legitimately use
    # "x,y" as a pair separator between two already-period-formatted numbers
    # (e.g. "104.0,198.0"), which is correct SVG and not what this guards.
    for name in ("x", "y", "cx", "cy", "r", "data-cx", "data-cy"):
        for value in re.findall(rf'\b{name}="([^"]*)"', html):
            assert "," not in value, f'{name}="{value}" would be invalid — a locale comma leaked in'
    # The bar's --pct custom property is the other place a comma silently
    # breaks scaleX().
    for pct in re.findall(r"--pct: ([^\"]+)", html):
        assert "," not in pct, f"--pct value {pct!r} would make scaleX() invalid CSS"


def test_dashboard_link_shown_only_to_owner(client, django_user_model):
    call_command("setup_roles")
    editor = django_user_model.objects.create_user("linkeditor", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)
    assert 'href="/dashboard/"' not in client.get("/pos/", follow=True).content.decode()

    client.force_login(django_user_model.objects.create_superuser("linkowner", password="x" * 12))
    assert 'href="/dashboard/"' in client.get("/pos/", follow=True).content.decode()


# ---- Storage / inventory dashboard (Owner-only) ---------------------------


def _editor(client, django_user_model, name):
    call_command("setup_roles")
    editor = django_user_model.objects.create_user(name, password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)
    return editor


def test_storage_page_is_owner_only(client, django_user_model, variant):
    # Anonymous -> login redirect, never rendered.
    assert client.get("/storage/").status_code == 302
    # Editor (staff, not superuser) -> hard 403.
    _editor(client, django_user_model, "storeeditor")
    assert client.get("/storage/").status_code == 403
    # Owner -> 200 with the real spreadsheet content.
    client.force_login(django_user_model.objects.create_superuser("storeowner", password="x" * 12))
    resp = client.get("/storage/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Склад" in body and "По категориям" in body and variant.sku in body


def test_storage_export_is_owner_only(client, django_user_model, variant):
    # Anonymous and Editor must never receive a file.
    assert client.get("/storage/export/?format=xlsx").status_code == 302
    _editor(client, django_user_model, "storeexpeditor")
    assert client.get("/storage/export/?format=xlsx").status_code == 403
    assert client.get("/storage/export/?format=csv&sheet=tovary").status_code == 403


def test_storage_export_xlsx_and_csv_for_owner(client, django_user_model, variant):
    client.force_login(django_user_model.objects.create_superuser("storeexp", password="x" * 12))
    add_movement(variant, StockMovement.PRODUCTION_IN, 12)

    xlsx = client.get("/storage/export/?format=xlsx")
    assert xlsx.status_code == 200
    assert "spreadsheetml.sheet" in xlsx["Content-Type"]
    assert "attachment; filename=" in xlsx["Content-Disposition"]
    assert xlsx.content[:2] == b"PK"  # a real .xlsx (zip) payload

    csv_resp = client.get("/storage/export/?format=csv&sheet=tovary")
    assert csv_resp.status_code == 200
    assert csv_resp["Content-Type"].startswith("text/csv")
    text = csv_resp.content.decode("utf-8")
    assert "Артикул" in text and variant.sku in text

    # An unknown csv sheet name is rejected, not silently served.
    assert client.get("/storage/export/?format=csv&sheet=secret").status_code == 403


def test_storage_link_shown_only_to_owner(client, django_user_model):
    _editor(client, django_user_model, "storelinkeditor")
    assert 'href="/storage/"' not in client.get("/pos/", follow=True).content.decode()
    client.force_login(
        django_user_model.objects.create_superuser("storelinkowner", password="x" * 12)
    )
    assert 'href="/storage/"' in client.get("/pos/", follow=True).content.decode()


def test_storage_aggregates_movements_by_type(variant):
    from apps.reports.storage import storage_data

    add_movement(variant, StockMovement.PRODUCTION_IN, 20)
    add_movement(variant, StockMovement.WRITEOFF_OUT, -3)  # defective
    add_movement(variant, StockMovement.RETURN_IN, 2)
    # a confirmed sale of 4 -> a sale_out movement of -4
    order = SaleOrder.objects.create()
    SaleItem.objects.create(order=order, variant=variant, quantity=4, unit_price=Decimal("100"))
    confirm_sale(order)

    data = storage_data()
    row = next(it for it in data["items"] if it["sku"] == variant.sku)
    assert row["intake"] == 20
    assert row["writeoff"] == 3  # shown as a positive count
    assert row["returns"] == 2
    assert row["sold"] == 4
    assert row["stock"] == 20 - 3 + 2 - 4  # ledger sum
    assert data["summary"]["sold"] == 4 and data["summary"]["writeoff"] == 3


def test_dashboard_export_is_owner_only(client, django_user_model):
    call_command("setup_roles")
    # Anonymous -> login redirect; Editor -> hard 403; neither gets a file.
    assert client.get("/dashboard/export/?format=xlsx").status_code == 302
    _editor(client, django_user_model, "dashexpeditor")
    assert client.get("/dashboard/export/?format=xlsx").status_code == 403
    assert client.get("/dashboard/export/?format=csv&sheet=tovary").status_code == 403


def test_dashboard_export_xlsx_and_csv_for_owner(client, django_user_model, variant):
    client.force_login(django_user_model.objects.create_superuser("dashexp", password="x" * 12))
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    order = SaleOrder.objects.create()
    SaleItem.objects.create(order=order, variant=variant, quantity=2, unit_price=Decimal("500"))
    confirm_sale(order)

    # xlsx: real workbook, filename carries the period.
    xlsx = client.get("/dashboard/export/?period=year&format=xlsx")
    assert xlsx.status_code == 200
    assert "spreadsheetml.sheet" in xlsx["Content-Type"]
    assert xlsx.content[:2] == b"PK"
    assert "year" in xlsx["Content-Disposition"]

    # csv of a single sheet.
    csv_resp = client.get("/dashboard/export/?period=year&format=csv&sheet=tovary")
    assert csv_resp.status_code == 200
    assert csv_resp["Content-Type"].startswith("text/csv")
    text = csv_resp.content.decode("utf-8")
    assert "Товар" in text and "Прибыль" in text

    # Unknown csv sheet -> rejected, never silently served.
    assert client.get("/dashboard/export/?format=csv&sheet=secret").status_code == 403


def test_dashboard_export_matches_the_page_numbers(variant):
    # download == screen: the export flattens the SAME dashboard_data().
    from apps.reports.dashboard import dashboard_data, dashboard_sheets

    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    order = SaleOrder.objects.create()
    SaleItem.objects.create(order=order, variant=variant, quantity=3, unit_price=Decimal("1000"))
    confirm_sale(order)  # revenue 3000 сом today

    data = dashboard_data("today")
    sheets = dashboard_sheets(data)
    svodka = dict(sheets["Сводка"][1:])  # {label: value}
    assert svodka["Выручка, сом"] == float(data["metrics"]["revenue"]["value"])
    assert svodka["Продано, шт"] == 3


def test_dashboard_export_link_is_period_aware(client, django_user_model, variant):
    client.force_login(django_user_model.objects.create_superuser("dashdl", password="x" * 12))
    body = client.get("/dashboard/?period=3m").content.decode()
    assert "/dashboard/export/?period=3m&amp;format=xlsx" in body


def test_dashboard_currency_toggle_is_view_only(client, django_user_model, variant):
    # ?cur=USD converts the SHOWN money at today's rate; units aren't money and
    # stay unchanged; the download always stays in сом (the money-truth file).
    from django.core.cache import cache
    from django.utils import timezone

    from apps.core.models import ExchangeRate

    cache.clear()  # the dated-rate cache bleeds across tests otherwise
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    order = SaleOrder.objects.create()
    SaleItem.objects.create(order=order, variant=variant, quantity=5, unit_price=Decimal("1000"))
    confirm_sale(order)  # 5000 сом today
    ExchangeRate.objects.create(
        currency="USD", date=timezone.localdate(), rate=Decimal("100"), source="nbkr"
    )
    client.force_login(django_user_model.objects.create_superuser("curown", password="x" * 12))

    som = client.get("/dashboard/?period=today").content.decode()
    assert "5\xa0000\xa0сом" in som

    usd = client.get("/dashboard/?period=today&cur=USD").content.decode()
    assert "50\xa0USD" in usd  # 5000 / 100, shown as a text abbreviation
    assert "5 шт" in usd  # units are not money — never converted
    assert "приблизительны" in usd  # the ≈ disclaimer

    # The export ignores the view currency — always сом.
    csv = client.get(
        "/dashboard/export/?period=today&cur=USD&format=csv&sheet=vyruchka"
    ).content.decode()
    assert "сом" in csv and "USD" not in csv


def test_dashboard_falls_back_to_som_without_a_rate(client, django_user_model, variant):
    from django.core.cache import cache

    cache.clear()  # ensure no rate leaked in from another test's dated-rate cache
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    order = SaleOrder.objects.create()
    SaleItem.objects.create(order=order, variant=variant, quantity=5, unit_price=Decimal("1000"))
    confirm_sale(order)
    client.force_login(django_user_model.objects.create_superuser("fbown", password="x" * 12))

    # Unknown currency code -> сом silently.
    unknown = client.get("/dashboard/?period=today&cur=ZZZ").content.decode()
    assert "5\xa0000\xa0сом" in unknown

    # A real currency with NO rate on record -> сом, but say so.
    no_rate = client.get("/dashboard/?period=today&cur=USD").content.decode()
    assert "5\xa0000\xa0сом" in no_rate
    assert "Курс валюты недоступен" in no_rate


def test_dashboard_omits_percentage_deltas(client, django_user_model):
    call_command("setup_roles")
    client.force_login(django_user_model.objects.create_superuser("nodelta", password="x" * 12))
    body = client.get("/dashboard/?period=today").content.decode()
    assert "metric__delta" not in body
    assert "ко вчера" not in body
    assert "нет данных за прошлый период" not in body


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
            SaleItem(
                order_id=o.pk,
                variant=variants[idx % len(variants)],
                quantity=1,
                unit_price=Decimal("1000"),
            )
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

    # Budget raised 12 -> 14 -> 16: the «Заказы» panels (Part 3g) added two
    # flat queries, and the CLIENT_BOTS.md panels (telegram_reach: 2 counts;
    # top_favourited: 1 aggregate + 1 variant lookup) add two more — all
    # still independent of sales volume, just a slightly higher fixed floor.
    for p in PERIODS:
        assert big[p] <= 16, f"{p}: {big[p]} queries at 5000 sales (budget 16)"
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

    record_payment(
        order, amount=Decimal("1200"), currency="KGS", method="cash"
    )  # Payment signal bumps
    debt_after = _cached_data("today")["metrics"]["debt"]["value"]
    assert debt_after == debt_before - Decimal("1200")


# ---------------------------------------------------------------------------
# Multi-currency payment hardening
# ---------------------------------------------------------------------------


def test_manager_can_refresh_rates_but_hand_entering_one_is_403(
    client, django_user_model, variant, monkeypatch
):
    """RATE PERMISSIONS: refreshing from NBKR is allowed for Editor/Manager;
    hand-entering/overriding a rate is Owner-only, enforced server-side even
    if a Manager bypasses the UI and POSTs it directly."""
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    manager = django_user_model.objects.create_user("mgr_perm", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)

    monkeypatch.setattr(
        "apps.core.management.commands.fetch_rates.requests.get", lambda *a, **k: _FakeResp()
    )
    assert client.post("/pos/rates/refresh/").status_code == 200  # allowed

    order = SaleOrder.objects.create(created_by=manager, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))

    resp = client.post(
        f"/pos/sale/{order.pk}/recalc/",
        {"amount": "10", "currency": "USD", "rate_override": "80"},
    )
    assert resp.status_code == 403

    resp = client.post(
        f"/pos/sale/{order.pk}/confirm/",
        {"amount": "10", "currency": "USD", "method": "cash", "rate_override": "80"},
    )
    assert resp.status_code == 403
    order.refresh_from_db()
    assert order.status == SaleOrder.DRAFT  # rejected before touching stock


def test_owner_rate_override_is_stored_with_official_rate_for_spread(
    client, django_user_model, variant, settings
):
    """OWNER RATE OVERRIDE: the Owner can hand-enter the actual (booth) rate —
    stored as rate_source=manual with rate_official (the NBKR rate at that
    moment) captured so the spread is reconstructable later."""
    settings.CURRENCY = "KGS"
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    owner = django_user_model.objects.create_superuser("owner_ov", "o@e.com", "x" * 12)
    client.force_login(owner)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    cust = Client.objects.create(first_name="Own", phone="+996700000811")
    client.post(f"/pos/sale/{order_id}/client/{cust.pk}/set/")
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})

    resp = client.post(
        f"/pos/sale/{order_id}/confirm/",
        {
            "amount": "10",
            "currency": "USD",
            "method": "cash",
            "rate_override": "89.50",
            "risk_ack": "1",  # a manual rate is itself a risk reason — needs ack
        },
    )
    assert resp.status_code == 302
    assert resp.url == f"/pos/sale/{order_id}/result/"

    order = SaleOrder.objects.get(pk=order_id)
    payment = order.payments.get()
    assert payment.rate_source == Payment.RATE_MANUAL
    assert payment.rate_to_kgs == Decimal("89.50")
    assert payment.rate_official == Decimal("87.000000")
    assert order.balance == Decimal("2305.00")  # 3200 - (10 × 89.50)


def test_manual_rate_deviating_from_official_warns_but_never_blocks(
    client, django_user_model, variant, settings
):
    settings.CURRENCY = "KGS"
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    owner = django_user_model.objects.create_superuser("owner_dev", "o@e.com", "x" * 12)
    client.force_login(owner)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    cust = Client.objects.create(first_name="Dev", phone="+996700000812")
    client.post(f"/pos/sale/{order_id}/client/{cust.pk}/set/")
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})

    # 95 vs 87 official = ~9.2% deviation — above the 5% warn threshold.
    resp = client.post(
        f"/pos/sale/{order_id}/recalc/",
        {"amount": "10", "currency": "USD", "rate_override": "95"},
    )
    assert "отличается от официального" in resp.content.decode()

    # The deviation itself never blocks — only the (separate) manual-rate risk
    # reason requires an ack, and ack'ing it lets the sale complete.
    resp = client.post(
        f"/pos/sale/{order_id}/confirm/",
        {
            "amount": "10",
            "currency": "USD",
            "method": "cash",
            "rate_override": "95",
            "risk_ack": "1",
        },
    )
    assert resp.status_code == 302
    payment = SaleOrder.objects.get(pk=order_id).payments.get()
    assert payment.rate_to_kgs == Decimal("95")


def test_void_foreign_payment_restores_debt_to_exact_pre_payment_value(variant, settings):
    """REVERSALS MUST USE THE FROZEN RATE: voiding a foreign payment restores
    the client's debt to EXACTLY its pre-payment value even after today's
    rate has moved significantly in between."""
    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    cust = Client.objects.create(first_name="Rev", phone="+996700000826")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("8700"))
    confirm_sale(order)
    debt_before = client_debt(cust)
    assert debt_before == {"KGS": Decimal("8700.00")}

    payment = Payment.objects.create(client=cust, order=order, amount=Decimal("10"), currency="USD")
    assert client_debt(cust) == {"KGS": Decimal("7830.00")}  # 8700 - (10 × 87)

    # The rate moves significantly AFTER the payment.
    ExchangeRate.objects.filter(currency="USD").update(rate=Decimal("150.00"))

    void_payment(payment)
    assert client_debt(cust) == debt_before  # exactly restored, unaffected by the new rate


def test_rates_card_shows_date_and_stale_badge_past_four_days(client, django_user_model, settings):
    settings.CURRENCY = "KGS"
    call_command("setup_roles")
    manager = django_user_model.objects.create_user("mgr_age", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)

    stale_date = timezone.localdate() - timezone.timedelta(days=5)
    ExchangeRate.objects.create(currency="USD", date=stale_date, rate=Decimal("87.00"))
    order = SaleOrder.objects.create(created_by=manager)

    resp = client.get(f"/pos/sale/{order.pk}/")
    body = resp.content.decode()
    assert stale_date.strftime("%d.%m.%Y") in body
    assert "Курс устарел" in body
    assert "badge--debt" in body


def test_scheduler_runs_fetch_rates_daily_and_before_the_report():
    """Restores the automatic daily pull (kept alongside the manual button) —
    automatic for reliability, manual for control."""
    import scheduler as scheduler_module

    job_commands = [cmds for _, cmds in scheduler_module.JOBS]
    assert ["fetch_rates"] in job_commands
    report_job = next(cmds for _, cmds in scheduler_module.JOBS if "send_daily_report" in cmds)
    assert "fetch_rates" in report_job
    assert "cleanup_draft_sales" in report_job


def test_refresh_rates_writes_audit_log_and_skips_noop_refresh(
    client, django_user_model, monkeypatch, settings
):
    settings.CURRENCY = "KGS"
    call_command("setup_roles")
    manager = django_user_model.objects.create_user("mgr_audit", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)

    monkeypatch.setattr(
        "apps.core.management.commands.fetch_rates.requests.get", lambda *a, **k: _FakeResp()
    )
    client.post("/pos/rates/refresh/")
    log = RateChangeLog.objects.get(currency="USD")
    assert log.old_rate is None
    assert log.new_rate == Decimal("87.45")
    assert log.source == ExchangeRate.NBKR
    assert log.changed_by == manager

    # Pulling back the SAME rate is not a "change" — no second log row.
    client.post("/pos/rates/refresh/")
    assert RateChangeLog.objects.filter(currency="USD").count() == 1


def test_exchange_rate_admin_is_owner_only(django_user_model):
    call_command("setup_roles")
    manager = django_user_model.objects.create_user("mgr_era", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    request = RequestFactory().get("/")
    request.user = manager
    model_admin = site._registry[ExchangeRate]
    assert model_admin.has_module_permission(request) is False
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False

    owner = django_user_model.objects.create_superuser("owner_era", "o@e.com", "x" * 12)
    request.user = owner
    assert model_admin.has_module_permission(request) is True
    assert model_admin.has_add_permission(request) is True
    assert model_admin.has_change_permission(request) is True


def test_rate_change_log_admin_is_read_only_and_owner_only(django_user_model):
    owner = django_user_model.objects.create_superuser("owner_rcl", "o@e.com", "x" * 12)
    request = RequestFactory().get("/")
    request.user = owner
    model_admin = site._registry[RateChangeLog]
    assert model_admin.has_view_permission(request) is True
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False

    staff = django_user_model.objects.create_user("staff_rcl", password="x" * 12, is_staff=True)
    request.user = staff
    assert model_admin.has_module_permission(request) is False


def test_exchange_rate_admin_save_model_upserts_and_logs(django_user_model):
    owner = django_user_model.objects.create_superuser("owner_save", "o@e.com", "x" * 12)
    request = RequestFactory().post("/")
    request.user = owner
    model_admin = site._registry[ExchangeRate]

    obj = ExchangeRate(currency="USD", rate=Decimal("89.00"))
    model_admin.save_model(request, obj, form=None, change=False)
    assert ExchangeRate.objects.filter(currency="USD").count() == 1
    row = ExchangeRate.objects.get(currency="USD")
    assert row.rate == Decimal("89.00")
    assert row.source == ExchangeRate.MANUAL
    log = RateChangeLog.objects.get(currency="USD")
    assert log.old_rate is None and log.new_rate == Decimal("89.00")
    assert log.source == ExchangeRate.MANUAL and log.changed_by == owner

    # Editing again upserts in place (still one row) and logs the change.
    obj2 = ExchangeRate(currency="USD", rate=Decimal("91.00"))
    model_admin.save_model(request, obj2, form=None, change=False)
    assert ExchangeRate.objects.filter(currency="USD").count() == 1
    assert RateChangeLog.objects.filter(currency="USD").count() == 2
    latest = RateChangeLog.objects.filter(currency="USD").order_by("-changed_at").first()
    assert latest.old_rate == Decimal("89.00") and latest.new_rate == Decimal("91.00")


def test_fresh_small_foreign_payment_confirms_without_risk_ack(
    client, django_user_model, variant, settings
):
    settings.CURRENCY = "KGS"
    settings.LARGE_PAYMENT_THRESHOLD_KGS = 10000
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    manager = django_user_model.objects.create_user("mgr_fresh", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    cust = Client.objects.create(first_name="Fresh", phone="+996700000820")
    client.post(f"/pos/sale/{order_id}/client/{cust.pk}/set/")
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})

    resp = client.post(
        f"/pos/sale/{order_id}/confirm/", {"amount": "10", "currency": "USD", "method": "cash"}
    )
    assert resp.status_code == 302
    assert resp.url == f"/pos/sale/{order_id}/result/"
    assert SaleOrder.objects.get(pk=order_id).payments.exists()


def test_stale_rate_payment_requires_explicit_risk_ack(
    client, django_user_model, variant, settings
):
    settings.CURRENCY = "KGS"
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    stale_date = timezone.localdate() - timezone.timedelta(days=5)
    ExchangeRate.objects.create(currency="USD", date=stale_date, rate=Decimal("87.00"))
    manager = django_user_model.objects.create_user("mgr_stale", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    cust = Client.objects.create(first_name="Stale", phone="+996700000821")
    client.post(f"/pos/sale/{order_id}/client/{cust.pk}/set/")
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})

    # Without risk_ack: rejected — the sale isn't even confirmed, nothing saved.
    resp = client.post(
        f"/pos/sale/{order_id}/confirm/", {"amount": "10", "currency": "USD", "method": "cash"}
    )
    assert resp.status_code == 302
    assert resp.url == f"/pos/sale/{order_id}/"
    order = SaleOrder.objects.get(pk=order_id)
    assert order.status == SaleOrder.DRAFT
    assert not order.payments.exists()

    # With risk_ack=1: goes through.
    resp = client.post(
        f"/pos/sale/{order_id}/confirm/",
        {"amount": "10", "currency": "USD", "method": "cash", "risk_ack": "1"},
    )
    assert resp.status_code == 302
    assert resp.url == f"/pos/sale/{order_id}/result/"
    order.refresh_from_db()
    assert order.status == SaleOrder.CONFIRMED
    assert order.payments.exists()


def test_large_converted_payment_requires_explicit_risk_ack(
    client, django_user_model, variant, settings
):
    settings.CURRENCY = "KGS"
    settings.LARGE_PAYMENT_THRESHOLD_KGS = 1000  # low threshold to trigger easily
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    manager = django_user_model.objects.create_user("mgr_big", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    cust = Client.objects.create(first_name="Big", phone="+996700000822")
    client.post(f"/pos/sale/{order_id}/client/{cust.pk}/set/")
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})

    # 20 USD × 87 = 1740 сом, above the 1000 сом threshold but below the 3200
    # сом order total — large, but NOT an overpayment (kept separate from the
    # сдача/overpayment-fork flow, which has its own dedicated tests).
    resp = client.post(
        f"/pos/sale/{order_id}/confirm/", {"amount": "20", "currency": "USD", "method": "cash"}
    )
    order = SaleOrder.objects.get(pk=order_id)
    assert order.status == SaleOrder.DRAFT
    assert not order.payments.exists()

    resp = client.post(
        f"/pos/sale/{order_id}/confirm/",
        {"amount": "20", "currency": "USD", "method": "cash", "risk_ack": "1"},
    )
    assert resp.status_code == 302
    order.refresh_from_db()
    assert order.status == SaleOrder.CONFIRMED


def test_missing_rate_at_confirm_shows_russian_error_and_saves_nothing(
    client, django_user_model, variant
):
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    manager = django_user_model.objects.create_user("mgr_norate", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)
    assert not ExchangeRate.objects.filter(currency="USD").exists()

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    cust = Client.objects.create(first_name="NoRate", phone="+996700000823")
    client.post(f"/pos/sale/{order_id}/client/{cust.pk}/set/")
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})

    resp = client.post(
        f"/pos/sale/{order_id}/confirm/", {"amount": "10", "currency": "USD", "method": "cash"}
    )
    assert resp.status_code == 302
    assert resp.url == f"/pos/sale/{order_id}/"
    order = SaleOrder.objects.get(pk=order_id)
    assert order.status == SaleOrder.DRAFT  # never confirmed either
    assert not order.payments.exists()
    page = client.get(resp.url)
    assert "Нет курса" in page.content.decode()


def test_record_payment_raises_for_missing_rate_and_saves_nothing(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="Svc", phone="+996700000824")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)
    assert not ExchangeRate.objects.filter(currency="USD").exists()
    with pytest.raises(ValidationError):
        record_payment(order, Decimal("10"), currency="USD")
    assert not order.payments.exists()


def test_same_currency_as_order_payment_uses_rate_one_and_skips_conversion_ui(
    client, django_user_model, variant, settings
):
    """order currency == payment currency (both USD, non-KGS) needs no rate
    lookup at all and shows no conversion math — even with zero ExchangeRate
    rows on record."""
    settings.CURRENCY = "KGS"
    call_command("setup_roles")
    variant.currency = "USD"
    variant.sale_price = Decimal("40")
    variant.save(update_fields=["currency", "sale_price"])
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    manager = django_user_model.objects.create_user("mgr_same", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)
    assert not ExchangeRate.objects.filter(currency="USD").exists()

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})
    assert SaleOrder.objects.get(pk=order_id).currency == "USD"

    resp = client.post(
        f"/pos/sale/{order_id}/recalc/", {"amount": "40", "currency": "USD", "method": "cash"}
    )
    body = resp.content.decode()
    assert "USD ×" not in body  # no conversion math for a same-currency payment
    assert "Нет курса" not in body  # and no missing-rate warning either

    resp = client.post(
        f"/pos/sale/{order_id}/confirm/", {"amount": "40", "currency": "USD", "method": "cash"}
    )
    assert resp.status_code == 302


def test_changing_rate_afterward_never_alters_past_payment_values(variant, settings):
    settings.CURRENCY = "KGS"
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    cust = Client.objects.create(first_name="Frz", phone="+996700000825")
    payment = Payment.objects.create(client=cust, amount=Decimal("10"), currency="USD")
    assert payment.rate_to_kgs == Decimal("87.000000")
    assert payment.amount_kgs == Decimal("870.00")

    ExchangeRate.objects.filter(currency="USD").update(rate=Decimal("95.00"))
    payment.refresh_from_db()
    assert payment.rate_to_kgs == Decimal("87.000000")  # unchanged
    assert payment.amount_kgs == Decimal("870.00")  # unchanged


def test_rounding_residue_marks_sale_paid_and_excluded_from_debts(variant, settings):
    settings.CURRENCY = "KGS"
    settings.PAYMENT_ROUNDING_TOLERANCE = Decimal("1.00")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="Round", phone="+996700000827")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("100.00"))
    confirm_sale(order)
    # Leaves a 0.04 сом residue — sub-сом currency-conversion rounding.
    record_payment(order, Decimal("99.96"), currency="KGS")
    assert order.balance == Decimal("0")
    assert order.payment_status == SaleOrder.PAID
    assert client_debt(cust) == {}


def test_payment_conversion_filter_shows_frozen_rate_math():
    from apps.pos.templatetags.pos_extras import payment_conversion_filter

    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.45"))
    cust = Client.objects.create(first_name="Fmt", phone="+996700000829")
    payment = Payment.objects.create(client=cust, amount=Decimal("10"), currency="USD")
    text = payment_conversion_filter(payment)
    assert "10 USD" in text
    assert "87.45" in text
    assert "874.50 KGS" in text
    assert "НБКР" in text

    same_currency_payment = Payment.objects.create(
        client=cust, amount=Decimal("100"), currency="KGS"
    )
    assert payment_conversion_filter(same_currency_payment) == ""


def test_daily_report_sales_sheet_includes_currency_rate_source_columns(variant, settings):
    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    cust = Client.objects.create(first_name="Rep", phone="+996700000828")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order)
    record_payment(order, Decimal("10"), currency="USD")  # 10 × 87 = 870

    rows = _sales_rows()
    header = rows[0]
    assert header[-5:] == [
        "Валюта оплаты",
        "Курс",
        "Источник курса",
        "Сдача",
        "Округление",
    ]
    data_row = rows[1]
    assert data_row[-5] == "USD"
    assert data_row[-4] == "87.0000"
    assert data_row[-3] == "НБКР"
    assert data_row[-2] == "—"  # no change given on this payment
    assert data_row[-1] == "—"  # no rounding residue either
    # Оплачено/Остаток use the CONVERTED paid amount, not a raw
    # same-currency-only sum (the bug this hardening pass fixed).
    assert data_row[9] == "870.00"  # Оплачено
    assert data_row[10] == "2330.00"  # Остаток


def test_client_admin_unpaid_orders_counts_converted_foreign_payment(variant, settings):
    """Regression: unpaid_orders() used to sum only same-currency payments,
    wrongly listing an order as unpaid even after a foreign payment fully
    settled it — the same bug class fixed in send_daily_report.py."""
    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    cust = Client.objects.create(first_name="AdmFx", phone="+996700000830")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("870"))
    confirm_sale(order)
    record_payment(order, Decimal("10"), currency="USD")  # 10 × 87 = 870 — fully settles it

    model_admin = site._registry[Client]
    assert str(model_admin.unpaid_orders(cust)) == "Нет — всё оплачено."


def test_rate_info_reflects_only_the_most_recently_fetched_value(settings):
    """DETERMINISM: one row per currency means exactly one current answer —
    a second fetch overwrites in place, never leaving two rates to disagree."""
    from apps.core.currency import rate_info

    settings.CURRENCY = "KGS"
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    assert rate_info("USD")["rate"] == Decimal("87.00")
    ExchangeRate.objects.filter(currency="USD").update(rate=Decimal("91.00"))
    assert rate_info("USD")["rate"] == Decimal("91.00")
    assert ExchangeRate.objects.filter(currency="USD").count() == 1


def test_no_float_in_money_and_conversion_code_paths():
    import ast
    import pathlib

    money_modules = [
        "apps/core/currency.py",
        "apps/sales/models.py",
        "apps/sales/services.py",
        "apps/clients/services.py",
        "apps/pos/views.py",
    ]
    base = pathlib.Path(__file__).resolve().parent.parent
    for rel in money_modules:
        source = (base / rel).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                pytest.fail(f"{rel}:{node.lineno} calls float() in a money/conversion path")


# ---------------------------------------------------------------------------
# Change (сдача) system
# ---------------------------------------------------------------------------


def test_same_currency_change_computes_and_rounds_correctly(variant, settings):
    settings.CURRENCY = "KGS"
    settings.CHANGE_ROUNDING_STEP = Decimal("1.00")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="Chg", phone="+996700001001")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("874.50"))
    confirm_sale(order)

    payment = record_payment(
        order,
        Decimal("1000"),
        currency="KGS",
        excess_disposition=Payment.DISPOSITION_CHANGE,
    )
    # Ideal change = 1000 - 874.50 = 125.50 -> floor to 125, residue 0.50.
    assert payment.change_amount == Decimal("125.00")
    assert payment.change_currency == "KGS"
    assert payment.change_amount_kgs == Decimal("125.00")
    assert payment.change_rounding_kgs == Decimal("0.50")
    # net = 1000 - 125 change = 875.00 (the 0.50 rounding residue stays with
    # the shop, since change was rounded DOWN — never given away silently).
    assert payment.net_applied_kgs == Decimal("875.00")
    order.refresh_from_db()
    assert order.balance == Decimal("0")
    assert order.payment_status == SaleOrder.PAID


def test_cross_currency_change_uses_the_same_frozen_rate_as_the_payment(variant, settings):
    settings.CURRENCY = "KGS"
    settings.CHANGE_ROUNDING_STEP = Decimal("1.00")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("80.00"))
    cust = Client.objects.create(first_name="ChgFx", phone="+996700001002")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order)

    # 20 USD x 80 = 1600 KGS toward a 1000 KGS sale -> 600 KGS ideal change,
    # already an exact step, no rounding residue.
    payment = record_payment(
        order,
        Decimal("20"),
        currency="USD",
        excess_disposition=Payment.DISPOSITION_CHANGE,
        change_currency="KGS",
    )
    assert payment.rate_to_kgs == Decimal("80.000000")
    assert payment.change_amount_kgs == Decimal("600.00")
    assert payment.change_amount == Decimal("600.00")  # change_currency == KGS, no conversion
    assert payment.net_applied_kgs == Decimal("1000.00")

    # The SAME payment, but change requested back in USD (the currency
    # received) — change_amount_kgs must be IDENTICAL (one rate, one
    # transaction); only its OWN-currency representation differs.
    order2 = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order2, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order2)
    payment2 = record_payment(
        order2,
        Decimal("20"),
        currency="USD",
        excess_disposition=Payment.DISPOSITION_CHANGE,
        change_currency="USD",
    )
    assert payment2.change_amount_kgs == Decimal("600.00")
    assert payment2.change_currency == "USD"
    assert payment2.change_amount == Decimal("7.50")  # 600 / 80


def test_change_while_balance_remains_is_rejected(variant, settings):
    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="NoChg", phone="+996700001003")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order)

    # A 500 payment against a 1000 total creates NO excess — forcing a change
    # override anyway (a crafted/buggy caller) must be rejected, not silently
    # accepted, since it would leave the sale not fully paid.
    with pytest.raises(ValidationError):
        record_payment(
            order,
            Decimal("500"),
            currency="KGS",
            excess_disposition=Payment.DISPOSITION_CHANGE,
            change_amount_override=Decimal("100"),
            change_adjust_reason="test override",
        )
    assert not order.payments.exists()


def test_negative_change_impossible_even_via_bulk_create(variant):
    from django.db import IntegrityError, transaction

    cust = Client.objects.create(first_name="Neg", phone="+996700001004")
    with pytest.raises(IntegrityError), transaction.atomic():
        Payment.objects.bulk_create(
            [
                Payment(
                    client=cust,
                    amount=Decimal("10"),
                    rate_to_kgs=Decimal("1"),
                    change_amount=Decimal("-5"),
                )
            ]
        )


def test_change_kgs_cannot_exceed_amount_kgs_even_via_bulk_create(variant):
    from django.db import IntegrityError, transaction

    cust = Client.objects.create(first_name="TooMuch", phone="+996700001005")
    with pytest.raises(IntegrityError), transaction.atomic():
        Payment.objects.bulk_create(
            [
                Payment(
                    client=cust,
                    amount=Decimal("10"),
                    rate_to_kgs=Decimal("1"),
                    change_amount=Decimal("5"),
                    change_amount_kgs=Decimal("20"),  # more than the 10 received
                )
            ]
        )


def test_net_applied_kgs_drives_debt_and_balance_everywhere(variant, settings):
    """CORE RULE: what reduces balance/debt is the NET, not the gross amount —
    checked at every layer: the order property, the client debt aggregate,
    and the admin's own annotated queryset."""
    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="Net", phone="+996700001006")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order)
    record_payment(
        order, Decimal("1500"), currency="KGS", excess_disposition=Payment.DISPOSITION_CHANGE
    )
    # net_applied_kgs = 1500 - 500 change = 1000 -> fully paid, no debt.
    order.refresh_from_db()
    assert order.paid_amount == Decimal("1000.00")
    assert order.balance == Decimal("0")
    assert client_debt(cust) == {}

    from django.contrib.admin.sites import site

    admin_order = site._registry[SaleOrder].get_queryset(RequestFactory().get("/")).get(pk=order.pk)
    assert admin_order._paid == Decimal("1000.00")


def test_debt_disposition_reduces_clients_other_outstanding_sales(variant, settings):
    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="Pool", phone="+996700001007")
    order_a = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order_a, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order_a)
    order_b = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order_b, variant=variant, quantity=1, unit_price=Decimal("500"))
    confirm_sale(order_b)
    assert client_debt(cust) == {"KGS": Decimal("1500.00")}

    # Pay 1500 on order A alone, marking the excess "в счёт долга" — the full
    # amount counts (no change), and the excess pools onto order B's debt too.
    record_payment(
        order_a, Decimal("1500"), currency="KGS", excess_disposition=Payment.DISPOSITION_DEBT
    )
    assert client_debt(cust) == {}


def test_credit_disposition_yields_negative_debt_shown_as_avans(variant, settings):
    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="Avans", phone="+996700001008")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order)

    record_payment(
        order, Decimal("1200"), currency="KGS", excess_disposition=Payment.DISPOSITION_CREDIT
    )
    assert client_debt(cust) == {}  # never shown as a negative debt
    assert client_credits(cust) == {"KGS": Decimal("200.00")}


def test_debt_and_credit_disposition_require_a_client(variant, settings):
    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="Req", phone="+996700001009")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order)
    # record_payment itself only guards client_id at the ORDER level (already
    # required elsewhere); this proves debt/credit compute cleanly with one.
    payment = record_payment(
        order, Decimal("1200"), currency="KGS", excess_disposition=Payment.DISPOSITION_DEBT
    )
    assert payment.excess_disposition == Payment.DISPOSITION_DEBT
    assert payment.change_amount == Decimal("0")


def test_void_payment_with_change_restores_exact_debt_after_rate_move(variant, settings):
    """DONE WHEN: a client hands 20 $ against an 874,50 сом sale — voiding
    that payment later restores the client's debt EXACTLY, even after the
    rate has since moved."""
    settings.CURRENCY = "KGS"
    settings.CHANGE_ROUNDING_STEP = Decimal("1.00")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.45"))
    cust = Client.objects.create(first_name="VoidChg", phone="+996700001010")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("874.50"))
    confirm_sale(order)
    debt_before = client_debt(cust)
    assert debt_before == {"KGS": Decimal("874.50")}

    payment = record_payment(
        order,
        Decimal("20"),
        currency="USD",
        excess_disposition=Payment.DISPOSITION_CHANGE,
    )
    assert payment.change_amount == Decimal("874.00")
    assert payment.change_rounding_kgs == Decimal("0.50")
    assert client_debt(cust) == {}  # fully paid, change given

    # The rate moves significantly AFTER the payment.
    ExchangeRate.objects.filter(currency="USD").update(rate=Decimal("150.00"))

    void_payment(payment)
    assert client_debt(cust) == debt_before  # exactly restored


def test_manager_out_of_band_change_amount_gets_403(client, django_user_model, variant, settings):
    settings.CURRENCY = "KGS"
    settings.CHANGE_ROUNDING_STEP = Decimal("1.00")
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    manager = django_user_model.objects.create_user("mgr_chg403", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    cust = Client.objects.create(first_name="Band", phone="+996700001011")
    client.post(f"/pos/sale/{order_id}/client/{cust.pk}/set/")
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})
    # variant sells for 3200 KGS; pay 4000 -> ideal change 800.

    resp = client.post(
        f"/pos/sale/{order_id}/recalc/",
        {
            "amount": "4000",
            "currency": "KGS",
            "excess_disposition": "change",
            "change_amount_override": "500",  # wildly outside the ±2 step band
        },
    )
    assert resp.status_code == 403

    resp = client.post(
        f"/pos/sale/{order_id}/confirm/",
        {
            "amount": "4000",
            "currency": "KGS",
            "method": "cash",
            "excess_disposition": "change",
            "change_amount_override": "500",
            "change_adjust_reason": "test",
        },
    )
    assert resp.status_code == 403
    order = SaleOrder.objects.get(pk=order_id)
    assert order.status == SaleOrder.DRAFT
    assert not order.payments.exists()


def test_owner_can_adjust_change_beyond_the_band(client, django_user_model, variant, settings):
    settings.CURRENCY = "KGS"
    settings.CHANGE_ROUNDING_STEP = Decimal("1.00")
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    owner = django_user_model.objects.create_superuser("owner_chg", "o@e.com", "x" * 12)
    client.force_login(owner)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    cust = Client.objects.create(first_name="OwnerBand", phone="+996700001012")
    client.post(f"/pos/sale/{order_id}/client/{cust.pk}/set/")
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})

    resp = client.post(
        f"/pos/sale/{order_id}/confirm/",
        {
            "amount": "4000",
            "currency": "KGS",
            "method": "cash",
            "excess_disposition": "change",
            "change_amount_override": "500",
            "change_adjust_reason": "owner discretion",
        },
    )
    assert resp.status_code == 302
    order = SaleOrder.objects.get(pk=order_id)
    assert order.status == SaleOrder.CONFIRMED
    payment = order.payments.get()
    assert payment.change_amount == Decimal("500")
    assert "owner discretion" in payment.note


def test_overpayment_requires_explicit_disposition_never_auto_picked(
    client, django_user_model, variant, settings
):
    settings.CURRENCY = "KGS"
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    manager = django_user_model.objects.create_user("mgr_fork", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    cust = Client.objects.create(first_name="Fork", phone="+996700001013")
    client.post(f"/pos/sale/{order_id}/client/{cust.pk}/set/")
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})

    # 4000 against a 3200 order, no disposition chosen -> rejected.
    resp = client.post(
        f"/pos/sale/{order_id}/confirm/", {"amount": "4000", "currency": "KGS", "method": "cash"}
    )
    assert resp.status_code == 302
    assert resp.url == f"/pos/sale/{order_id}/"
    order = SaleOrder.objects.get(pk=order_id)
    assert order.status == SaleOrder.DRAFT
    assert not order.payments.exists()

    # The double-check panel shows the math once "Сдача" is picked.
    resp = client.post(
        f"/pos/sale/{order_id}/recalc/",
        {"amount": "4000", "currency": "KGS", "excess_disposition": "change"},
    )
    body = resp.content.decode()
    assert "800" in body  # computed change (4000 - 3200)
    assert "Подтвердить и выдать сдачу" in body

    # Confirming with the disposition now succeeds and hands back the change.
    resp = client.post(
        f"/pos/sale/{order_id}/confirm/",
        {
            "amount": "4000",
            "currency": "KGS",
            "method": "cash",
            "excess_disposition": "change",
        },
    )
    assert resp.status_code == 302
    assert resp.url == f"/pos/sale/{order_id}/result/"
    order.refresh_from_db()
    assert order.status == SaleOrder.CONFIRMED
    payment = order.payments.get()
    assert payment.change_amount == Decimal("800.00")
    assert payment.excess_disposition == Payment.DISPOSITION_CHANGE
    assert order.payment_status == SaleOrder.PAID


def test_debt_credit_fork_buttons_disabled_for_walkin(client, django_user_model, variant, settings):
    settings.CURRENCY = "KGS"
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    manager = django_user_model.objects.create_user("mgr_walkin", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1})

    resp = client.post(f"/pos/sale/{order_id}/recalc/", {"amount": "4000", "currency": "KGS"})
    body = resp.content.decode()
    assert "В счёт долга" in body and "Аванс" in body
    # Both fork buttons carrying "debt"/"credit" are disabled for a walk-in.
    assert body.count("disabled") >= 2


def test_client_credits_display_as_avans_on_client_page(
    client, django_user_model, variant, settings
):
    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    call_command("setup_roles")
    manager = django_user_model.objects.create_user("mgr_page", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)

    cust = Client.objects.create(first_name="PageAvans", phone="+996700001014")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order)
    record_payment(
        order, Decimal("1200"), currency="KGS", excess_disposition=Payment.DISPOSITION_CREDIT
    )

    resp = client.get(f"/pos/clients/{cust.pk}/")
    body = resp.content.decode()
    assert "Аванс" in body
    assert "200" in body


def test_daily_report_change_and_rounding_columns_and_till_drift_total(variant, settings):
    settings.CURRENCY = "KGS"
    settings.CHANGE_ROUNDING_STEP = Decimal("1.00")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="ReportChg", phone="+996700001015")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("874.50"))
    confirm_sale(order)
    record_payment(
        order, Decimal("1000"), currency="KGS", excess_disposition=Payment.DISPOSITION_CHANGE
    )

    rows = _sales_rows()
    header = rows[0]
    assert header[-2:] == ["Сдача", "Округление"]
    data_row = rows[1]
    assert data_row[-2] == "125.00"
    assert data_row[-1] == "0.50"
    summary_row = rows[-1]
    assert "Округление за день" in summary_row[-1]
    assert "0.50" in summary_row[-1]


def test_balance_kgs_before_payment_matches_order_balance_after_confirm(variant, settings):
    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="Bal", phone="+996700001016")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order)
    assert balance_kgs_before_payment(order) == Decimal("1000.00")
    record_payment(order, Decimal("400"), currency="KGS")
    assert balance_kgs_before_payment(order) == Decimal("600.00")


def test_compute_change_preview_has_excess_flag_respects_tolerance(variant, settings):
    settings.CURRENCY = "KGS"
    settings.PAYMENT_ROUNDING_TOLERANCE = Decimal("1.00")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="Tol", phone="+996700001017")
    order = SaleOrder.objects.create(client=cust, currency="KGS")
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("1000"))
    confirm_sale(order)
    # 1000.50 over a 1000 balance is within tolerance -> no excess/fork needed.
    preview = compute_change_preview(
        order, Decimal("1000.50"), "KGS", Decimal("1"), balance_kgs_before_payment(order)
    )
    assert preview["has_excess"] is False


# ---------------------------------------------------------------------------
# Part 1 — stock cannot go negative (cart-time cap + reservation)
# ---------------------------------------------------------------------------


def test_cart_cap_applies_across_all_lines_of_the_same_variant(client, django_user_model, variant):
    """Adding the same variant twice (two separate POSTs) must not bypass the
    cap — it's checked against the TOTAL already in the cart, not per add."""
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    editor = django_user_model.objects.create_user("cap1", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 7})
    resp = client.post(
        f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 7}
    )
    body = resp.content.decode()
    assert "Доступно только" in body
    order = SaleOrder.objects.get(pk=order_id)
    assert order.items.count() == 1
    assert order.items.first().quantity == 10  # capped at the 10 available, not 14


def test_zero_available_tile_cannot_be_added_even_via_crafted_post(
    client, django_user_model, variant
):
    call_command("setup_roles")
    # No stock movement at all — 0 available.
    editor = django_user_model.objects.create_user("cap2", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    resp = client.post(
        f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1}
    )
    assert (
        "уже полностью в корзине" in resp.content.decode()
        or "Доступно только" in resp.content.decode()
    )
    order = SaleOrder.objects.get(pk=order_id)
    assert order.items.count() == 0


def test_crafted_post_above_stock_is_clamped_not_saved_beyond_available(
    client, django_user_model, variant
):
    """A crafted POST with qty far above stock is rejected — clamped to what's
    truly available, never silently saved at face value. Client-side caps are
    UX only; this is the server-side defence."""
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 15)
    editor = django_user_model.objects.create_user("cap3", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    resp = client.post(
        f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 999999}
    )
    assert resp.status_code == 200
    assert "Доступно только 15" in resp.content.decode()
    order = SaleOrder.objects.get(pk=order_id)
    assert order.items.first().quantity == 15  # never the crafted 999999


def test_reserved_stock_is_excluded_from_available_and_blocks_a_walkin_sale(variant, settings):
    """Part 1b/3c: stock promised to an open production order must not be
    sellable to a walk-in, even bypassing the cart-time cap entirely."""
    from apps.inventory.services import available_for
    from apps.orders.services import create_order

    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    cust = Client.objects.create(first_name="Reserver", phone="+996700002101")
    create_order(cust, [{"variant": variant, "quantity": 8, "unit_price": variant.sale_price}])
    assert available_for(variant) == 2  # 10 on hand - 8 reserved

    # A sale attempting to take more than what's left over the reservation
    # must fail at confirm time, even if the cart itself somehow held it.
    walkin = SaleOrder.objects.create()
    SaleItem.objects.create(
        order=walkin, variant=variant, quantity=3, unit_price=variant.sale_price
    )
    with pytest.raises(ValidationError):
        confirm_sale(walkin)
    variant.refresh_from_db()
    assert variant.stock == 10  # nothing written off


def test_product_grid_tile_shows_reserved_badge(client, django_user_model, variant, settings):
    from apps.orders.services import create_order

    settings.CURRENCY = "KGS"
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    cust = Client.objects.create(first_name="Badge", phone="+996700002102")
    create_order(cust, [{"variant": variant, "quantity": 3, "unit_price": variant.sale_price}])
    editor = django_user_model.objects.create_user("cap4", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    resp = client.get(f"/pos/sale/{order_id}/products/")
    body = resp.content.decode()
    assert "7" in body  # 10 - 3 reserved = 7 available
    assert "в заказах" in body


# ---------------------------------------------------------------------------
# Part 2 — money formatting + cart rail
# ---------------------------------------------------------------------------


def test_money_filter_groups_thousands_with_nbsp_and_drops_trailing_zero_cents():
    from apps.pos.templatetags.pos_extras import money_filter

    assert money_filter(Decimal("3800"), "KGS") == "3\xa0800\xa0сом"
    assert money_filter(Decimal("3800.00"), "KGS") == "3\xa0800\xa0сом"
    assert money_filter(Decimal("874.50"), "KGS") == "874,50\xa0сом"
    assert money_filter(Decimal("-500"), "KGS") == "-500\xa0сом"


def test_money_filter_uses_currency_symbol_not_code():
    from apps.pos.templatetags.pos_extras import money_filter

    assert money_filter(Decimal("100"), "USD") == "100\xa0$"
    assert money_filter(Decimal("100"), "RUB") == "100\xa0₽"
    assert "USD" not in money_filter(Decimal("100"), "USD")
    assert "KGS" not in money_filter(Decimal("100"), "KGS")


def test_sale_screen_renders_formatted_money_not_raw_currency_code(
    client, django_user_model, variant
):
    call_command("setup_roles")
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    editor = django_user_model.objects.create_user("fmt1", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    resp = client.get("/pos/")
    order_id = int(resp.url.rstrip("/").rsplit("/", 1)[-1])
    resp = client.post(
        f"/pos/sale/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 1}
    )
    body = resp.content.decode()
    assert "3800,00 KGS" not in body
    assert "3\xa0200\xa0сом" in body  # variant sells for 3200 KGS


def test_cart_rail_keeps_confirm_button_and_money_bar_at_25_items(
    client, django_user_model, settings
):
    """Server-side proxy for the layout requirement: with 25 lines in the
    cart, the money bar and confirm button must still be present in the
    response — CSS (tested manually/visually) keeps them pinned/visible
    regardless of item count; this proves the markup itself never drops
    them when the list grows long."""
    settings.CURRENCY = "KGS"
    call_command("setup_roles")
    editor = django_user_model.objects.create_user("rail1", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(editor)

    cat = Category.objects.create(name="Rail")
    order_resp = client.get("/pos/")
    order_id = int(order_resp.url.rstrip("/").rsplit("/", 1)[-1])
    for i in range(25):
        p = Product.objects.create(category=cat, name=f"RailProduct{i}")
        v = ProductVariant.objects.create(
            product=p, sku=f"RAIL{i:03d}", cost_price=Decimal("1"), sale_price=Decimal("100")
        )
        add_movement(v, StockMovement.PRODUCTION_IN, 5)
        resp = client.post(f"/pos/sale/{order_id}/items/add/", {"variant_id": v.pk, "quantity": 1})
    body = resp.content.decode()
    order = SaleOrder.objects.get(pk=order_id)
    assert order.items.count() == 25
    assert "Позиции · 25" in body
    assert 'class="money-bar' in body
    assert "Подтвердить продажу" in body
    assert "items-scroll" in body


# ---------------------------------------------------------------------------
# Part 3 — Заказы (production orders)
# ---------------------------------------------------------------------------


def test_production_queue_need_is_ordered_minus_stock_never_negative_displayed(variant, settings):
    from apps.orders.services import create_order, production_queue

    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 3)
    cust = Client.objects.create(first_name="Queue1", phone="+996700002201")
    create_order(cust, [{"variant": variant, "quantity": 10, "unit_price": variant.sale_price}])

    rows = production_queue()
    row = next(r for r in rows if r["variant"].pk == variant.pk)
    assert row["ordered"] == 10
    assert row["in_stock"] == 3
    assert row["to_produce"] == 7
    assert row["covered"] is False

    # Now stock covers demand — need clamps to 0, flagged covered, not hidden.
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    rows = production_queue()
    row = next(r for r in rows if r["variant"].pk == variant.pk)
    assert row["to_produce"] == 0
    assert row["covered"] is True


def test_queue_aggregates_the_same_variant_across_two_orders(variant, settings):
    from apps.orders.services import create_order, production_queue

    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 2)
    c1 = Client.objects.create(first_name="QA", phone="+996700002202")
    c2 = Client.objects.create(first_name="QB", phone="+996700002203")
    create_order(c1, [{"variant": variant, "quantity": 4, "unit_price": variant.sale_price}])
    create_order(c2, [{"variant": variant, "quantity": 6, "unit_price": variant.sale_price}])

    rows = production_queue()
    matching = [r for r in rows if r["variant"].pk == variant.pk]
    assert len(matching) == 1  # aggregated into ONE row, not two
    row = matching[0]
    assert row["ordered"] == 10  # 4 + 6
    assert row["to_produce"] == 8  # 10 - 2 in stock
    assert row["orders_count"] == 2


def test_queue_row_breaks_down_who_ordered_what(variant, settings):
    """«Для кого» — the queue must answer which client is waiting on how many
    without opening each order, and each line's `remaining` is that LINE's own
    shortfall, not the variant's stock-adjusted need."""
    from apps.orders.services import create_order, production_queue

    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 2)
    c1 = Client.objects.create(first_name="Breakdown1", phone="+996700002251")
    c2 = Client.objects.create(first_name="Breakdown2", phone="+996700002252")
    create_order(c1, [{"variant": variant, "quantity": 4, "unit_price": variant.sale_price}])
    create_order(c2, [{"variant": variant, "quantity": 6, "unit_price": variant.sale_price}])

    row = next(r for r in production_queue() if r["variant"].pk == variant.pk)
    assert row["clients_count"] == 2
    assert len(row["lines"]) == 2
    by_client = {line["client"].first_name: line for line in row["lines"]}
    assert by_client["Breakdown1"]["quantity"] == 4
    assert by_client["Breakdown1"]["remaining"] == 4  # nothing produced on that line yet
    assert by_client["Breakdown2"]["quantity"] == 6
    # The 2 already in stock reduce the VARIANT's need, never a client's line.
    assert row["to_produce"] == 8


def test_queue_groups_by_product_and_by_client(variant, settings):
    """Same arithmetic, three views of it. Grouping never invents or loses
    units: the per-size split sums back to the product row."""
    from apps.orders.services import create_order, production_queue

    settings.CURRENCY = "KGS"
    other_size = ProductVariant.objects.create(
        product=variant.product,
        sku="EVD-L-RED",
        size="L",
        color="red",
        cost_price=Decimal("1500.00"),
        sale_price=Decimal("3200.00"),
    )
    cust = Client.objects.create(first_name="Grouped", phone="+996700002253")
    create_order(
        cust,
        [
            {"variant": variant, "quantity": 3, "unit_price": variant.sale_price},
            {"variant": other_size, "quantity": 2, "unit_price": other_size.sale_price},
        ],
    )

    # Two SKUs -> two variant rows, but ONE product row holding both.
    assert len({r["key"] for r in production_queue("variant")}) == 2
    product_rows = production_queue("product")
    assert len(product_rows) == 1
    prow = product_rows[0]
    assert prow["to_produce"] == 5  # 3 + 2, nothing in stock
    assert len(prow["variants"]) == 2
    assert sum(v["to_produce"] for v in prow["variants"]) == prow["to_produce"]

    # One client -> one client row listing both sizes they're waiting on.
    client_rows = production_queue("client")
    assert len(client_rows) == 1
    crow = client_rows[0]
    assert crow["client"].pk == cust.pk
    assert crow["remaining"] == 5
    assert len(crow["lines"]) == 2


def test_queue_sorts_by_nearest_due_date_and_flags_overdue(variant, settings):
    """Nearest deadline first, undated last, overdue flagged against the
    Asia/Bishkek LOCAL date (never UTC) — same rule as Order.is_overdue."""
    from datetime import timedelta

    from apps.orders.services import create_order, production_queue

    settings.CURRENCY = "KGS"
    today = timezone.localdate()
    sizes = {}
    for label, due in (("late", today - timedelta(days=2)), ("soon", today + timedelta(days=3))):
        sizes[label] = ProductVariant.objects.create(
            product=variant.product,
            sku=f"EVD-{label}",
            size=label,
            color="red",
            cost_price=Decimal("1500.00"),
            sale_price=Decimal("3200.00"),
        )
        cust = Client.objects.create(first_name=label, phone=f"+99670000226{len(sizes)}")
        create_order(
            cust,
            [{"variant": sizes[label], "quantity": 1, "unit_price": Decimal("3200.00")}],
            due_date=due,
        )
    # A third order with no due date at all must sink below both.
    undated = Client.objects.create(first_name="undated", phone="+996700002269")
    create_order(undated, [{"variant": variant, "quantity": 1, "unit_price": variant.sale_price}])

    rows = production_queue()
    assert [r["variant"].size for r in rows] == ["late", "soon", "M"]
    assert rows[0]["overdue"] is True
    assert rows[1]["overdue"] is False
    assert rows[2]["due_date"] is None and rows[2]["overdue"] is False


def test_queue_view_accepts_group_param_and_ignores_garbage(
    client, django_user_model, variant, settings
):
    settings.CURRENCY = "KGS"
    client.force_login(
        django_user_model.objects.create_superuser("queue_owner", "q@e.com", "x" * 12)
    )
    cust = Client.objects.create(first_name="ViewGroup", phone="+996700002270")
    from apps.orders.services import create_order

    create_order(cust, [{"variant": variant, "quantity": 2, "unit_price": variant.sale_price}])

    for group in ("variant", "product", "client"):
        resp = client.get(f"/orders/queue/?group={group}")
        assert resp.status_code == 200
        assert resp.context["group"] == group
        assert "ViewGroup" in resp.content.decode()
    # An unknown grouping falls back to the default rather than 500ing.
    resp = client.get("/orders/queue/?group=../etc/passwd")
    assert resp.status_code == 200
    assert resp.context["group"] == "variant"


def test_orders_index_status_filter_separates_open_from_delivered(
    client, variant, django_user_model, settings
):
    """The «where did my order go?» fix: the list defaults to the working set,
    and a delivered order moves out of it instead of burying today's work."""
    from apps.orders.models import Order
    from apps.orders.services import create_order, hand_over

    settings.CURRENCY = "KGS"
    owner = django_user_model.objects.create_superuser("orders_owner", "oo@e.com", "x" * 12)
    client.force_login(owner)
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="Filtered", phone="+996700002271")
    open_order = create_order(
        cust, [{"variant": variant, "quantity": 1, "unit_price": variant.sale_price}]
    )
    done = create_order(
        cust, [{"variant": variant, "quantity": 1, "unit_price": variant.sale_price}]
    )
    hand_over(done, user=owner)

    resp = client.get("/orders/")  # default = активные
    assert resp.context["status"] == "open"
    ids = [r["order"].pk for r in resp.context["rows"]]
    assert open_order.pk in ids and done.pk not in ids
    assert resp.context["counts"]["open"] == 1
    assert resp.context["counts"]["delivered"] == 1

    resp = client.get("/orders/?status=delivered")
    ids = [r["order"].pk for r in resp.context["rows"]]
    assert ids == [done.pk]

    resp = client.get("/orders/?status=all")
    ids = [r["order"].pk for r in resp.context["rows"]]
    assert open_order.pk in ids and done.pk in ids

    # Garbage falls back to the default instead of erroring.
    assert client.get("/orders/?status=nonsense").context["status"] == "open"
    assert Order.objects.count() == 2


def test_order_builder_swaps_in_place_for_htmx_and_redirects_without_it(
    client, django_user_model, variant, settings
):
    """Building an order used to be a full page load per item. Every control
    now swaps #order-body, and the same endpoint still redirects for a plain
    POST — the swap is an enhancement, not the mechanism."""
    from apps.orders.services import create_order

    settings.CURRENCY = "KGS"
    owner = django_user_model.objects.create_superuser("swap_owner", "s@e.com", "x" * 12)
    client.force_login(owner)
    cust = Client.objects.create(first_name="Swap", phone="+996700002291")
    order = create_order(
        cust, [{"variant": variant, "quantity": 1, "unit_price": variant.sale_price}]
    )
    add_url = f"/orders/{order.pk}/items/add/"

    # No JS: a normal POST still redirects back to the order.
    resp = client.post(add_url, {"variant_id": variant.pk, "quantity": 1})
    assert resp.status_code == 302

    # HTMX: a fragment comes back instead, and it closes the picker modal
    # out-of-band so the next item can be added without leaving the page.
    resp = client.post(add_url, {"variant_id": variant.pk, "quantity": 1}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert not body.lstrip().startswith("<!doctype")
    assert 'id="variant-picker" hx-swap-oob' in body
    assert str(variant) in body  # the line list came back with it


def test_order_builder_shows_errors_inside_the_swapped_body(
    client, django_user_model, variant, settings
):
    """A swapped fragment never reaches base.html's message block, so an
    over-produce that used to go to messages.error would vanish silently."""
    from apps.orders.services import create_order

    settings.CURRENCY = "KGS"
    owner = django_user_model.objects.create_superuser("err_owner", "er@e.com", "x" * 12)
    client.force_login(owner)
    cust = Client.objects.create(first_name="ErrBody", phone="+996700002292")
    order = create_order(
        cust, [{"variant": variant, "quantity": 2, "unit_price": variant.sale_price}]
    )
    item = order.items.get()

    resp = client.post(
        f"/orders/{order.pk}/items/{item.pk}/produce/",
        {"quantity": 99},  # more than the line has left
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    assert 'class="error"' in resp.content.decode()
    item.refresh_from_db()
    assert item.produced_qty == 0  # and nothing was written


def test_empty_order_hides_money_sections_until_it_has_items(
    client, django_user_model, variant, settings
):
    """Аванс and Итого are meaningless against a 0 сом order — they appear
    only once there's something to price."""
    from apps.orders.models import Order

    settings.CURRENCY = "KGS"
    owner = django_user_model.objects.create_superuser("empty_owner", "em@e.com", "x" * 12)
    client.force_login(owner)
    cust = Client.objects.create(first_name="EmptyOrd", phone="+996700002293")
    order = Order.objects.create(client=cust, created_by=owner)

    body = client.get(f"/orders/{order.pk}/").content.decode()
    assert "Внести аванс" not in body
    assert "Аванс внесён" not in body  # the totals card is out too
    assert "найдите товар выше" in body  # ...replaced by what to do next

    client.post(f"/orders/{order.pk}/items/add/", {"variant_id": variant.pk, "quantity": 1})
    body = client.get(f"/orders/{order.pk}/").content.decode()
    assert "Внести аванс" in body
    assert "Аванс внесён" in body


def test_guided_creation_walks_items_then_due_then_payment_then_saves(
    client, django_user_model, variant, settings
):
    """A new order takes the guided route: товары → срок → оплата → сохранено,
    the last two in dialogs, ending back on the list with a success flash."""
    settings.CURRENCY = "KGS"
    owner = django_user_model.objects.create_superuser("wiz_owner", "w@e.com", "x" * 12)
    client.force_login(owner)
    cust = Client.objects.create(first_name="Wizard", phone="+996700002311")

    resp = client.post("/orders/new/", {"client": cust.pk})
    order_id = int(re.search(r"/orders/(\d+)/", resp.url).group(1))
    assert "new=1" in resp.url  # a new order starts on the guided route

    # HTMX posts don't carry the page's query string; htmx sends the current
    # URL instead, which is how the swapped body stays in wizard mode.
    hx = {"HTTP_HX_REQUEST": "true", "HTTP_HX_CURRENT_URL": f"http://t/orders/{order_id}/?new=1"}

    body = client.get(f"/orders/{order_id}/?new=1").content.decode()
    assert "Далее: срок и оплата" in body
    assert "Внести аванс" not in body  # аванс is step 3, not an inline card
    assert "Завершение" not in body  # can't hand over an order being created

    client.post(f"/orders/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 5}, **hx)

    body = client.get(f"/orders/{order_id}/step/due/", **hx).content.decode()
    assert "Шаг 2 из 3" in body
    # Saving the срок hands straight on to step 3 in the same slot.
    body = client.post(
        f"/orders/{order_id}/step/due/", {"due_date": "2026-09-15", "note": "к свадьбе"}, **hx
    ).content.decode()
    assert "Шаг 3 из 3" in body
    assert "станет долгом клиента после выдачи" in body

    resp = client.post(
        f"/orders/{order_id}/step/payment/",
        {"amount": "2000", "currency": "KGS", "method": "cash"},
        **hx,
    )
    assert resp.status_code == 204
    assert resp["HX-Redirect"] == "/orders/"

    from apps.orders.models import Order

    order = Order.objects.get(pk=order_id)
    assert str(order.due_date) == "2026-09-15"
    assert order.note == "к свадьбе"
    assert order.deposits.count() == 1

    body = client.get("/orders/").content.decode()
    assert f"Заказ №{order_id} сохранён." in body
    assert "flash--ok" in body  # a success reads as success, not as an error


def test_handover_moves_money_stock_and_debt_exactly_once(
    client, django_user_model, variant, settings
):
    """The whole chain the order flow promises, asserted end to end: an open
    order owes nothing, handover deducts stock once and turns the remainder
    into real client debt, the deposit carries over, and paying the rest
    clears the debt while both payments stay on the sale as history."""
    from apps.clients.services import client_debt
    from apps.inventory.services import available_for
    from apps.orders.models import Order
    from apps.orders.services import create_order, hand_over, record_deposit
    from apps.sales.models import SaleOrder

    settings.CURRENCY = "KGS"
    owner = django_user_model.objects.create_superuser("chain_owner", "c@e.com", "x" * 12)
    cust = Client.objects.create(first_name="ChainFlow", phone="+996700002312")
    order = create_order(
        cust, [{"variant": variant, "quantity": 5, "unit_price": Decimal("1000")}], user=owner
    )
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)

    # An open заказ is NOT a sale: it owes nothing and reserves its stock.
    assert client_debt(cust) == {}
    assert available_for(variant) == 0  # reserved, unsellable to a walk-in
    record_deposit(order, Decimal("2000"), user=owner)
    assert client_debt(cust) == {}  # a deposit on an order is still not debt

    sale = hand_over(order, user=owner)
    variant.refresh_from_db()
    assert variant.stock == 0  # deducted exactly once, by confirm_sale
    assert sale.total == Decimal("5000.00")
    assert sale.paid_amount == Decimal("2000.00")  # the deposit carried over
    assert sale.balance == Decimal("3000.00")
    assert client_debt(cust) == {"KGS": Decimal("3000.00")}  # remainder IS the debt

    order.refresh_from_db()
    assert order.status == Order.DELIVERED
    assert order.sale_order_id == sale.pk  # linked both ways
    assert sale.production_order.pk == order.pk

    record_payment(sale, Decimal("3000"), user=owner, method=Payment.CASH)
    sale.refresh_from_db()
    assert sale.balance == 0
    assert sale.payment_status == SaleOrder.PAID
    assert client_debt(cust) == {}  # debt cleared
    # Both the deposit and the final payment live on the sale as history.
    assert sorted(sale.payments.values_list("amount", flat=True)) == [
        Decimal("2000.00"),
        Decimal("3000.00"),
    ]


def test_order_line_progress_reports_real_stock_and_other_orders_claims(variant, settings):
    """Per-line the order page must show real numbers: this line's own
    production progress, the variant's actual stock, and how much of that
    stock another open order has already been promised."""
    from apps.orders.services import create_order, mark_produced, order_line_progress

    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 30)
    mine = Client.objects.create(first_name="Mine", phone="+996700002301")
    rival = Client.objects.create(first_name="Rival", phone="+996700002302")
    order = create_order(
        mine, [{"variant": variant, "quantity": 10, "unit_price": variant.sale_price}]
    )

    row = order_line_progress(order)[0]
    assert row["in_stock"] == 30
    assert row["claimed_by_others"] == 0
    assert row["free_for_order"] == 30
    assert row["coverable"] is True  # 30 on hand covers this line's 10
    assert row["made"] == 0 and row["percent"] == 0

    # Another open order books 25 of the same SKU — only 5 are really free now.
    create_order(rival, [{"variant": variant, "quantity": 25, "unit_price": variant.sale_price}])
    row = order_line_progress(order)[0]
    assert row["in_stock"] == 30  # unchanged fact
    assert row["claimed_by_others"] == 25
    assert row["free_for_order"] == 5
    assert row["coverable"] is False  # 5 free < 10 ordered

    # Taking 4 into stock moves this line's own progress to 40%.
    mark_produced(order.items.get(), 4)
    row = order_line_progress(order)[0]
    assert row["made"] == 4
    assert row["percent"] == 40
    assert row["remaining"] == 6


def test_shortfall_is_about_stock_not_production_progress(variant, settings):
    """A fully produced line can still be short at handover, because another
    open order holds a prior claim on the same shared stock. The shortfall
    shown must be the STOCK gap — reporting the production remainder printed
    the nonsense «принято 20 из 20 · 100%» next to «не хватает 0 шт»."""
    from apps.orders.services import create_order, mark_produced, order_line_progress

    settings.CURRENCY = "KGS"
    mine = Client.objects.create(first_name="ShortMine", phone="+996700002306")
    rival = Client.objects.create(first_name="ShortRival", phone="+996700002307")
    order = create_order(
        mine, [{"variant": variant, "quantity": 20, "unit_price": variant.sale_price}]
    )
    create_order(rival, [{"variant": variant, "quantity": 100, "unit_price": variant.sale_price}])

    # Produce this line in full: 100% made, and those 20 units are in stock...
    mark_produced(order.items.get(), 20)
    row = order_line_progress(order)[0]
    assert row["percent"] == 100
    assert row["remaining"] == 0  # nothing left to sew
    # ...but the rival order's 100-unit claim outweighs the 20 on hand, so
    # handover would still fail — and the number said so must be the stock gap.
    assert row["free_for_order"] == 0
    assert row["coverable"] is False
    assert row["short_by"] == 20  # not 0, which is the production remainder


def test_order_progress_percentage_rolls_up_across_lines(variant, settings):
    from apps.orders.services import create_order, mark_produced, order_progress

    settings.CURRENCY = "KGS"
    other = ProductVariant.objects.create(
        product=variant.product,
        sku="EVD-PROG-L",
        size="L",
        color="red",
        cost_price=Decimal("1500.00"),
        sale_price=Decimal("3200.00"),
    )
    cust = Client.objects.create(first_name="Prog", phone="+996700002303")
    order = create_order(
        cust,
        [
            {"variant": variant, "quantity": 3, "unit_price": variant.sale_price},
            {"variant": other, "quantity": 1, "unit_price": other.sale_price},
        ],
    )
    assert order_progress(order) == {"made": 0, "total": 4, "percent": 0, "complete": False}

    mark_produced(order.items.get(variant=variant), 3)
    prog = order_progress(order)
    assert prog["made"] == 3 and prog["total"] == 4 and prog["percent"] == 75
    assert prog["complete"] is False

    mark_produced(order.items.get(variant=other), 1)
    assert order_progress(order)["complete"] is True


def test_saving_the_due_date_finishes_and_returns_to_the_list(
    client, django_user_model, variant, settings
):
    """Setting the срок is the last step of creating an order, so it saves
    and goes back to the list — both for a plain POST and under HTMX (which
    needs HX-Redirect, or the whole list page would be pasted into the body)."""
    from apps.orders.services import create_order

    settings.CURRENCY = "KGS"
    owner = django_user_model.objects.create_superuser("due_owner", "d@e.com", "x" * 12)
    client.force_login(owner)
    cust = Client.objects.create(first_name="DueDate", phone="+996700002304")
    order = create_order(
        cust, [{"variant": variant, "quantity": 1, "unit_price": variant.sale_price}]
    )
    url = f"/orders/{order.pk}/due-date/"

    resp = client.post(url, {"due_date": "2026-09-01", "note": "подшить"})
    assert resp.status_code == 302
    assert resp.url == "/orders/"
    order.refresh_from_db()
    assert str(order.due_date) == "2026-09-01"
    assert order.note == "подшить"

    resp = client.post(url, {"due_date": "2026-09-02", "note": ""}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 204  # nothing to swap
    assert resp["HX-Redirect"] == "/orders/"


def test_handover_dialog_blocks_when_stock_cannot_cover_the_order(
    client, django_user_model, variant, settings
):
    """confirm_sale raises on an oversell anyway; the dialog's job is to say so
    BEFORE the manager taps, instead of after."""
    from apps.orders.services import create_order

    settings.CURRENCY = "KGS"
    owner = django_user_model.objects.create_superuser("hand_owner", "h@e.com", "x" * 12)
    client.force_login(owner)
    cust = Client.objects.create(first_name="Handover", phone="+996700002305")
    order = create_order(
        cust, [{"variant": variant, "quantity": 5, "unit_price": variant.sale_price}]
    )

    # Nothing produced yet -> the dialog warns and its submit is disabled.
    resp = client.get(f"/orders/{order.pk}/deliver/confirm/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert len(resp.context["short_lines"]) == 1
    assert "Не хватает остатка" in body
    assert "disabled" in body

    # Take the goods into stock -> the same dialog now allows the handover.
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    resp = client.get(f"/orders/{order.pk}/deliver/confirm/")
    assert resp.context["short_lines"] == []
    assert "Не хватает остатка" not in resp.content.decode()
    assert resp.context["remaining"] == order.total  # no deposit taken


def test_order_page_offers_a_way_out_that_is_not_handing_over(
    client, django_user_model, variant, settings
):
    """«Выдать заказ» is irreversible — it confirms a sale and deducts stock.
    It may only LOOK like the default action once the order is готов; until
    then leaving the page must be the visually obvious move, or a manager
    trying to finish editing reaches for the destructive button."""
    import re

    from apps.orders.models import Order
    from apps.orders.services import create_order, mark_produced

    settings.CURRENCY = "KGS"
    owner = django_user_model.objects.create_superuser("exit_owner", "e@e.com", "x" * 12)
    client.force_login(owner)
    cust = Client.objects.create(first_name="ExitRoute", phone="+996700002281")
    order = create_order(
        cust, [{"variant": variant, "quantity": 2, "unit_price": variant.sale_price}]
    )

    def deliver_button_class(body):
        # type="button" — it opens the handover summary dialog rather than
        # submitting; the styling is what this test is about.
        m = re.search(r'<button type="button"\s+class="btn (btn-\w+)"[^>]*>\s*Выдать заказ', body)
        assert m, "the Выдать заказ button disappeared"
        return m.group(1)

    body = client.get(f"/orders/{order.pk}/").content.decode()
    assert "back-link" in body  # an escape hatch above the fold
    assert "К заказам" in body  # ...and one next to the money actions
    assert deliver_button_class(body) == "btn-ghost"  # not the default-looking action

    # Once everything is produced the order is готов and handing it over IS
    # the natural next step, so it earns the primary styling.
    for item in order.items.all():
        mark_produced(item, item.quantity, user=owner)
    order.refresh_from_db()
    assert order.status == Order.READY
    body = client.get(f"/orders/{order.pk}/").content.decode()
    assert deliver_button_class(body) == "btn-primary"
    assert "К заказам" in body  # the exit never goes away


def test_header_wordmark_links_home(client, django_user_model, settings):
    """The ACOCOS wordmark is the way back to the terminal from any /pos/
    screen — the convention every web app already trained people on."""
    settings.CURRENCY = "KGS"
    client.force_login(
        django_user_model.objects.create_superuser("brand_owner", "b@e.com", "x" * 12)
    )
    body = client.get("/pos/today/").content.decode()
    assert '<a class="top__brand" href="/pos/"' in body


def test_admin_panel_links_back_to_the_pos_terminal(client, django_user_model):
    """/panel/ used to be a one-way door — the POS header links in and nothing
    linked back, so the only way out was editing the URL by hand."""
    client.force_login(
        django_user_model.objects.create_superuser("panel_owner", "p@e.com", "x" * 12)
    )
    body = client.get("/panel/").content.decode()
    assert "Терминал ACOCOS" in body
    assert 'href="/pos/"' in body


def test_mark_produced_creates_movement_and_raises_available(variant, django_user_model, settings):
    from apps.inventory.services import available_for
    from apps.orders.models import Order
    from apps.orders.services import create_order, mark_produced

    settings.CURRENCY = "KGS"
    cust = Client.objects.create(first_name="Prod1", phone="+996700002204")
    order = create_order(
        cust, [{"variant": variant, "quantity": 5, "unit_price": variant.sale_price}]
    )
    item = order.items.get()
    assert available_for(variant) == -5  # nothing on hand yet, 5 fully reserved

    mark_produced(item, 2)
    item.refresh_from_db()
    assert item.produced_qty == 2
    # Producing raises on_hand, but the reservation stays the FULL 5 (not
    # reduced by produced_qty) — the produced units are still earmarked for
    # this order, not sellable to a walk-in: available is unchanged.
    assert available_for(variant) == -3  # 2 on hand - 5 reserved
    assert variant.stock == 2  # PRODUCTION_IN movement written
    order.refresh_from_db()
    assert order.status == Order.IN_PRODUCTION

    mark_produced(item, 3)
    item.refresh_from_db()
    order.refresh_from_db()
    assert item.produced_qty == 5
    assert order.status == Order.READY  # fully produced -> auto-advance
    assert order.fully_produced is True


def test_mark_produced_rejects_more_than_remaining(variant):
    from apps.orders.services import create_order, mark_produced

    cust = Client.objects.create(first_name="Prod2", phone="+996700002205")
    order = create_order(
        cust, [{"variant": variant, "quantity": 3, "unit_price": variant.sale_price}]
    )
    item = order.items.get()
    with pytest.raises(ValidationError):
        mark_produced(item, 4)
    item.refresh_from_db()
    assert item.produced_qty == 0


def test_handover_deducts_stock_once_applies_deposit_and_links_order_and_sale(variant, settings):
    from apps.orders.models import Order
    from apps.orders.services import create_order, hand_over, mark_produced, record_deposit

    settings.CURRENCY = "KGS"
    cust = Client.objects.create(first_name="Handover1", phone="+996700002206")
    order = create_order(cust, [{"variant": variant, "quantity": 4, "unit_price": Decimal("1000")}])
    item = order.items.get()
    mark_produced(item, 4)
    variant.refresh_from_db()
    assert variant.stock == 4

    deposit = record_deposit(order, Decimal("1500"), currency="KGS")
    assert deposit.production_order_id == order.pk
    assert deposit.order_id is None  # not linked to a sale yet

    sale_out_before = StockMovement.objects.filter(
        movement_type=StockMovement.SALE_OUT, variant=variant
    ).count()

    sale = hand_over(order)

    sale_out_after = StockMovement.objects.filter(
        movement_type=StockMovement.SALE_OUT, variant=variant
    ).count()
    assert sale_out_after == sale_out_before + 1  # stock deducted EXACTLY once
    variant.refresh_from_db()
    assert variant.stock == 0

    order.refresh_from_db()
    assert order.status == Order.DELIVERED
    assert order.sale_order_id == sale.pk
    assert sale.production_order.pk == order.pk  # linked both ways

    deposit.refresh_from_db()
    assert deposit.order_id == sale.pk  # deposit carried over
    assert sale.paid_amount == Decimal("1500.00")
    assert sale.balance == Decimal("2500.00")  # 4000 total - 1500 deposit


def test_handover_rejects_an_order_with_no_items(variant):
    from apps.orders.services import hand_over
    from apps.orders.models import Order

    cust = Client.objects.create(first_name="Empty", phone="+996700002207")
    order = Order.objects.create(client=cust)
    with pytest.raises(ValidationError):
        hand_over(order)


def test_cancel_order_releases_its_reservation(variant, settings):
    from apps.inventory.services import available_for
    from apps.orders.services import cancel_order, create_order

    settings.CURRENCY = "KGS"
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="Cancel1", phone="+996700002208")
    order = create_order(
        cust, [{"variant": variant, "quantity": 5, "unit_price": variant.sale_price}]
    )
    assert available_for(variant) == 0

    cancel_order(order)
    assert available_for(variant) == 5  # reservation released
    order.refresh_from_db()
    from apps.orders.models import Order

    assert order.status == Order.CANCELLED


def test_overdue_detection_respects_bishkek_local_date_not_utc(monkeypatch):
    from datetime import date

    from apps.orders.models import Order

    cust = Client.objects.create(first_name="OverdueTZ", phone="+996700002209")
    order = Order.objects.create(client=cust, due_date=date(2026, 7, 17))
    with timezone.override("Asia/Bishkek"):
        # 00:30 Bishkek on the 17th == 18:30 UTC on the 16th — the LOCAL date
        # is already the due date, not yet overdue.
        _freeze_now(monkeypatch, 2026, 7, 16, 18, 30)
        assert order.is_overdue is False
        # 00:30 Bishkek on the 18th == 18:30 UTC on the 17th — a UTC-based
        # check would still read the 17th (not overdue); the correct local
        # date is the 18th, one day past due.
        _freeze_now(monkeypatch, 2026, 7, 17, 18, 30)
        assert order.is_overdue is True


def test_deposit_reuses_the_frozen_rate_mechanism_no_second_conversion_path(variant, settings):
    settings.CURRENCY = "KGS"
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    from apps.orders.services import create_order, record_deposit

    cust = Client.objects.create(first_name="DepositFx", phone="+996700002210")
    order = create_order(cust, [{"variant": variant, "quantity": 1, "unit_price": Decimal("1000")}])
    deposit = record_deposit(order, Decimal("10"), currency="USD")
    assert deposit.rate_to_kgs == Decimal("87.000000")
    assert deposit.amount_kgs == Decimal("870.00")

    # Changing the rate afterward never alters the stored deposit — same
    # invariant as every other frozen-rate payment in the system.
    ExchangeRate.objects.filter(currency="USD").update(rate=Decimal("150.00"))
    deposit.refresh_from_db()
    assert deposit.rate_to_kgs == Decimal("87.000000")


def test_owner_only_can_cancel_order_via_admin(django_user_model):
    from django.contrib.admin.sites import site

    from apps.orders.models import Order

    cust = Client.objects.create(first_name="AdminCancel", phone="+996700002211")
    order = Order.objects.create(client=cust)
    model_admin = site._registry[Order]
    request = RequestFactory().post("/")
    manager = django_user_model.objects.create_user("mgr_cancel", password="x" * 12, is_staff=True)
    request.user = manager

    from django.contrib.messages.storage.fallback import FallbackStorage

    setattr(request, "session", {})
    messages_storage = FallbackStorage(request)
    setattr(request, "_messages", messages_storage)

    model_admin.cancel_selected(request, Order.objects.filter(pk=order.pk))
    order.refresh_from_db()
    assert order.status == Order.NEW  # unchanged — Editor can't cancel

    owner = django_user_model.objects.create_superuser("owner_cancel", "o@e.com", "x" * 12)
    request.user = owner
    model_admin.cancel_selected(request, Order.objects.filter(pk=order.pk))
    order.refresh_from_db()
    assert order.status == Order.CANCELLED


def test_done_when_scenario_order_deposit_queue_produce_handover(
    client, django_user_model, variant, settings
):
    """DONE WHEN: create an order for unproduced goods, take a deposit in any
    currency, see it in the queue, mark produced, hand over as a normal sale
    that deducts stock exactly once."""
    settings.CURRENCY = "KGS"
    ExchangeRate.objects.create(currency="USD", date=timezone.localdate(), rate=Decimal("87.00"))
    call_command("setup_roles")
    manager = django_user_model.objects.create_user("done_mgr", password="x" * 12, is_staff=True)
    manager.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(manager)
    assert variant.stock == 0  # nothing produced yet — the whole point

    cust = Client.objects.create(first_name="DoneWhen", phone="+996700002212")
    # Order creation is POST-only (a cross-site GET must not be able to spin up
    # orders) — the client-picker row is a CSRF-protected form.
    resp = client.post("/orders/new/", {"client": cust.pk})
    assert resp.status_code == 302
    # /orders/<id>/?new=1 — a new order lands on the guided creation route.
    order_id = int(re.search(r"/orders/(\d+)/", resp.url).group(1))
    assert "new=1" in resp.url

    # No cap on ordering unproduced goods.
    resp = client.post(f"/orders/{order_id}/items/add/", {"variant_id": variant.pk, "quantity": 5})
    assert resp.status_code == 302

    # Deposit in a foreign currency.
    client.post(
        f"/orders/{order_id}/deposit/", {"amount": "20", "currency": "USD", "method": "cash"}
    )

    from apps.orders.models import Order

    order = Order.objects.get(pk=order_id)
    assert order.deposits.count() == 1

    # Shows up in the aggregated queue, with the client it's being made for.
    resp = client.get("/orders/queue/")
    body = resp.content.decode()
    assert "сшить" in body  # the row's headline number
    assert "DoneWhen" in body  # ...and who it's for

    # Mark produced.
    item = order.items.get()
    client.post(f"/orders/{order_id}/items/{item.pk}/produce/", {"quantity": 5})
    variant.refresh_from_db()
    assert variant.stock == 5

    # Hand over -> a normal confirmed sale, stock deducted exactly once.
    resp = client.post(f"/orders/{order_id}/deliver/")
    assert resp.status_code == 302
    variant.refresh_from_db()
    assert variant.stock == 0
    order.refresh_from_db()
    assert order.status == Order.DELIVERED
    assert order.sale_order is not None
    assert order.sale_order.status == SaleOrder.CONFIRMED
