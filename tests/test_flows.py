from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.clients.models import Client
from apps.clients.services import client_debt
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
        product=product, sku="EVD-M-RED", size="M", color="red",
        cost_price=Decimal("1500.00"), sale_price=Decimal("3200.00"),
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
