"""URL-security regressions: state-mutating endpoints must be POST-only, so a
cross-site GET (an <img>/link a logged-in manager is tricked into loading)
cannot trigger them — GET carries no CSRF token, and a GET-write would bypass
CSRF entirely. Every mutation endpoint returns 405 on GET; the few
legitimately-mixed GET/POST views (sale_return form, notes edit/index) render
on GET and only mutate on POST. Read-only renders (search, product grid) stay
GET.
"""

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.clients.models import Client
from apps.inventory.models import Category, Product, ProductVariant, StockMovement
from apps.inventory.services import add_movement
from apps.orders.models import Order
from apps.sales.models import SaleOrder

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(client, django_user_model):
    call_command("setup_roles")
    user = django_user_model.objects.create_superuser("sec_owner", "o@example.com", "x" * 12)
    client.force_login(user)
    return user


@pytest.fixture
def variant():
    cat = Category.objects.create(name="Dresses")
    product = Product.objects.create(category=cat, name="Evening Dress")
    return ProductVariant.objects.create(
        product=product,
        sku="EVD-M-RED",
        size="M",
        color="red",
        cost_price=Decimal("1500"),
        sale_price=Decimal("3200"),
    )


# ---- POS mutation endpoints reject GET (405) -------------------------------


def test_pos_mutation_endpoints_reject_get(client, owner, variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    order = SaleOrder.objects.create(created_by=owner, status=SaleOrder.DRAFT)
    c = Client.objects.create(first_name="X", phone="+996700000001")

    get_only_405 = [
        reverse("pos:client_set", args=[order.pk, c.pk]),
        reverse("pos:client_clear", args=[order.pk]),
        reverse("pos:client_create", args=[order.pk]),
        reverse("pos:item_add", args=[order.pk]),
        reverse("pos:recalc", args=[order.pk]),
        reverse("pos:sale_confirm", args=[order.pk]),
        reverse("pos:refresh_rates"),
    ]
    for url in get_only_405:
        assert client.get(url).status_code == 405, f"GET {url} should be 405"


def test_pos_confirm_via_get_does_not_confirm_the_sale(client, owner, variant):
    """The headline attack: a GET to /confirm/ used to run confirm_sale with an
    empty payment. It must now 405 and leave the draft untouched."""
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    order = SaleOrder.objects.create(created_by=owner, status=SaleOrder.DRAFT)
    resp = client.get(f"/pos/sale/{order.pk}/confirm/")
    assert resp.status_code == 405
    order.refresh_from_db()
    assert order.status == SaleOrder.DRAFT  # never confirmed


def test_pos_cancel_via_get_does_not_cancel(client, owner, variant):
    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    order = SaleOrder.objects.create(created_by=owner, status=SaleOrder.DRAFT)
    # confirm it properly (POST) first
    client.post(f"/pos/sale/{order.pk}/items/add/", {"variant_id": variant.pk, "quantity": 1})
    client.post(f"/pos/sale/{order.pk}/confirm/", {"amount": "0"})
    order.refresh_from_db()
    assert order.status == SaleOrder.CONFIRMED

    assert client.get(f"/pos/sale/{order.pk}/cancel/").status_code == 405
    order.refresh_from_db()
    assert order.status == SaleOrder.CONFIRMED  # still confirmed, not cancelled


def test_pos_readonly_endpoints_still_accept_get(client, owner, variant):
    order = SaleOrder.objects.create(created_by=owner, status=SaleOrder.DRAFT)
    for url in [
        reverse("pos:client_search", args=[order.pk]),
        reverse("pos:product_grid", args=[order.pk]),
        reverse("pos:today"),
        reverse("pos:clients"),
    ]:
        assert client.get(url).status_code == 200, f"GET {url} should still render"


# ---- Orders mutation endpoints reject GET ----------------------------------


def test_orders_mutation_endpoints_reject_get(client, owner, variant):
    c = Client.objects.create(first_name="O", phone="+996700000002")
    order = Order.objects.create(client=c, created_by=owner)
    item = order.items.create(
        variant=variant, quantity=2, unit_price=Decimal("3200"), currency="KGS"
    )
    for url in [
        reverse("orders:item_add", args=[order.pk]),
        reverse("orders:item_remove", args=[order.pk, item.pk]),
        reverse("orders:produce", args=[order.pk, item.pk]),
        reverse("orders:set_due_date", args=[order.pk]),
        reverse("orders:deposit_add", args=[order.pk]),
        reverse("orders:deliver", args=[order.pk]),
        reverse("orders:cancel", args=[order.pk]),
        reverse("orders:client_create"),
    ]:
        assert client.get(url).status_code == 405, f"GET {url} should be 405"


def test_orders_create_via_get_does_not_create_an_order(client, owner):
    """orders:create used to spin up a real Order row on a GET with ?client=.
    A GET must now only render the picker (200) and create nothing."""
    c = Client.objects.create(first_name="P", phone="+996700000003")
    before = Order.objects.count()
    resp = client.get(f"/orders/new/?client={c.pk}")
    assert resp.status_code == 200  # renders the picker, does not redirect to a new order
    assert Order.objects.count() == before  # nothing created

    # The real flow: a CSRF-protected POST creates it.
    resp = client.post("/orders/new/", {"client": c.pk})
    assert resp.status_code == 302
    assert Order.objects.count() == before + 1


def test_orders_readonly_endpoints_still_accept_get(client, owner):
    c = Client.objects.create(first_name="Q", phone="+996700000004")
    order = Order.objects.create(client=c, created_by=owner)
    for url in [
        reverse("orders:index"),
        reverse("orders:queue"),
        reverse("orders:detail", args=[order.pk]),
        reverse("orders:client_search"),
    ]:
        assert client.get(url).status_code == 200, f"GET {url} should still render"


# ---- Inbox mutation endpoints reject GET (flags on so routes exist) --------


def test_inbox_mutation_endpoints_reject_get(client, owner, variant, settings):
    settings.BOTS_ENABLED = True
    c = Client.objects.create(first_name="R", phone="+996700000005")
    from apps.inbox.models import OrderRequest

    req = OrderRequest.objects.create(client=c, source=OrderRequest.TELEGRAM)
    for url in [
        reverse("inbox:reply", args=[c.pk]),
        reverse("inbox:request_confirm", args=[req.pk]),
        reverse("inbox:request_decline", args=[req.pk]),
    ]:
        assert client.get(url).status_code == 405, f"GET {url} should be 405"


# ---- theme cookie: still works, and is Secure over HTTPS -------------------


def test_theme_cookie_is_secure_over_https(client):
    resp = client.post("/theme/", {"theme": "dark", "next": "/pos/"}, secure=True)
    assert resp.status_code == 302
    assert resp.cookies["theme"]["secure"]
    assert resp.cookies["theme"]["httponly"]


def test_theme_endpoint_rejects_get(client):
    assert client.get("/theme/").status_code == 405


# ---- deploy: /healthz/ must be exempt from the prod https redirect ----------


def test_healthz_exempt_from_ssl_redirect_but_other_paths_still_forced(settings, rf):
    """The web container's Docker healthcheck probes http://localhost:8000/healthz/
    from inside the container (no Caddy, no X-Forwarded-Proto). With
    SECURE_SSL_REDIRECT on, that probe must NOT be 301'd to https (which would
    fail the check and leave the container permanently 'unhealthy'), while every
    other path still gets forced to https. A fresh SecurityMiddleware compiles
    SECURE_REDIRECT_EXEMPT from the current settings, so this test is reliable."""
    from django.http import HttpResponse
    from django.middleware.security import SecurityMiddleware

    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]
    mw = SecurityMiddleware(lambda req: HttpResponse("ok"))

    healthz = mw(rf.get("/healthz/"))
    assert healthz.status_code == 200  # not redirected

    other = mw(rf.get("/pos/"))
    assert other.status_code == 301
    assert other["Location"].startswith("https://")
