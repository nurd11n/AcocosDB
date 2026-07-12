from decimal import Decimal

import pytest
from django.contrib.admin.sites import site
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory

from apps.clients.models import Client, Interaction
from apps.clients.services import client_debt, log_whatsapp_interaction
from apps.core.management.commands.send_daily_report import _debts_rows, _sales_rows, _stock_rows
from apps.core.permissions import EDITOR, VIEWER
from apps.inventory.models import Category, Product, ProductVariant, StockMovement
from apps.inventory.services import add_movement, adjust_to_count
from apps.sales.models import Payment, SaleItem, SaleOrder
from apps.sales.services import cancel_sale, confirm_sale

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
    client = Client.objects.create(name="Aisha", phone="+996700000001")
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
    client = Client.objects.create(name="Meerim", phone="+996700000002")
    order = SaleOrder.objects.create(client=client)
    SaleItem.objects.create(order=order, variant=variant, quantity=2, unit_price=Decimal("3200"))
    confirm_sale(order)
    Payment.objects.create(client=client, order=order, amount=Decimal("2000"))
    assert client_debt(client) == Decimal("4400")


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


def test_whatsapp_message_auto_creates_client_and_interaction():
    client = log_whatsapp_interaction("+996700000099", "hello")
    assert client.source == Client.WHATSAPP
    assert client.interactions.count() == 1
    assert client.interactions.first().kind == Interaction.MESSAGE

    # A second message from the same number reuses the client, doesn't duplicate it.
    log_whatsapp_interaction("+996700000099", "again")
    assert Client.objects.filter(phone="+996700000099").count() == 1
    assert client.interactions.count() == 2


def test_daily_report_rows_reflect_current_data(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    client = Client.objects.create(name="Aiperi", phone="+996700000003")
    order = SaleOrder.objects.create(client=client, channel=SaleOrder.SHOP)
    SaleItem.objects.create(order=order, variant=variant, quantity=2, unit_price=Decimal("3200"))
    confirm_sale(order)
    Payment.objects.create(client=client, order=order, amount=Decimal("1000"))

    sales = _sales_rows()
    assert sales[0][0] == "Time"
    assert any(row[1] == "Aiperi" for row in sales[1:-1])

    stock = _stock_rows()
    assert stock[0] == [
        "SKU",
        "Product",
        "Size",
        "Color",
        "Stock",
        "Sale Price",
        "Cost Price",
        "Stock Value",
        "Low",
    ]
    assert any(row[0] == variant.sku and row[4] == 8 for row in stock[1:])

    debts = _debts_rows()
    assert debts[0][0] == "Name"
    assert any(row[0] == "Aiperi" and row[2] == "5400" for row in debts[1:])


def test_send_daily_report_command_skips_network_when_unconfigured(capsys):
    call_command("send_daily_report")
    out = capsys.readouterr().out.lower()
    assert "skipped" in out


def test_payment_status_reflects_payments(variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    c = Client.objects.create(name="Nur", phone="+996700000004")
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
