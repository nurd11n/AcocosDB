"""Exhaustive role × surface probe.

The suite already covers method-safety (test_url_security) and individual
role rules scattered across test_flows. What was missing is one place that
walks EVERY sensitive surface against EVERY role and asserts the whole matrix
at once — so a newly-added Owner-only page can't quietly ship reachable by a
Viewer just because nobody wrote its individual test.

Owner-only surfaces come straight from CLAUDE.md's Roles section: Система,
cost prices, profit, campaigns, the dashboard/storage/stats analytics pages,
and everything under /panel/.
"""

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command

from apps.core.permissions import EDITOR, VIEWER

pytestmark = pytest.mark.django_db

# Surfaces only the Owner (superuser) may open at all.
OWNER_ONLY = [
    "/dashboard/",
    "/dashboard/export/",
    "/storage/",
    "/storage/export/",
    "/stats/",
    "/stats/download/?format=csv",
    # NOTE: /panel/ ITSELF is deliberately staff-reachable — Editor/Viewer use
    # it for business models (CLAUDE.md's Roles section); what they must never
    # reach is Система + campaigns + cost/profit data, listed below.
    "/panel/campaigns/campaign/",
    "/panel/core/exchangerate/",
    "/panel/core/ratechangelog/",
    "/panel/manufacturing/expense/",
    "/panel/manufacturing/contractor/",
    "/panel/auth/user/",
    "/manufacturing/",  # contractors list (empty path in manufacturing/urls.py)
    "/manufacturing/expenses/",
    "/manufacturing/dashboard/",
    "/panel/personal/personalexpense/",
    "/personal/",
    "/personal/export/",
]

# Surfaces any logged-in staff member may open (read at minimum).
STAFF_READABLE = ["/pos/", "/pos/today/", "/pos/clients/", "/orders/"]


@pytest.fixture
def roles(db):
    call_command("setup_roles")


def _mk(django_user_model, username, group=None, superuser=False):
    if superuser:
        return django_user_model.objects.create_superuser(username, f"{username}@e.com", "x" * 12)
    u = django_user_model.objects.create_user(username, password="x" * 12, is_staff=True)
    if group:
        u.groups.add(Group.objects.get(name=group))
    return u


@pytest.mark.parametrize("url", OWNER_ONLY)
def test_owner_only_surfaces_reject_viewer_and_editor(client, django_user_model, roles, url):
    """A non-Owner must never get a 200 here — 403 or a redirect to /login/,
    never the page itself. Typing the URL directly is the whole threat model:
    these are never linked for them."""
    for username, group in (("mx_viewer", VIEWER), ("mx_editor", EDITOR)):
        user = _mk(django_user_model, username, group=group)
        client.force_login(user)
        resp = client.get(url)
        assert resp.status_code != 200, f"{group} reached {url} (got 200)"
        assert resp.status_code in (302, 403, 404), f"{group} on {url}: {resp.status_code}"
        client.logout()


@pytest.mark.parametrize("url", OWNER_ONLY)
def test_owner_only_surfaces_reject_anonymous(client, roles, url):
    resp = client.get(url)
    assert resp.status_code != 200, f"anonymous reached {url}"


@pytest.mark.parametrize(
    "url",
    # /panel/campaigns/campaign/ is excluded on purpose: with CAMPAIGNS_ENABLED
    # False (the shipped default) CampaignAdmin refuses even the Owner — that
    # is the flag working, not a broken page. Its own 403-for-Owner behaviour
    # is covered in tests/test_prod_flags.py.
    [u for u in OWNER_ONLY if u != "/panel/campaigns/campaign/"],
)
def test_owner_can_open_every_owner_surface(client, django_user_model, roles, url):
    """The other half of the matrix: these must actually WORK for the Owner —
    a test that only proves "everyone is blocked" would pass on a broken page."""
    owner = _mk(django_user_model, "mx_owner", superuser=True)
    client.force_login(owner)
    resp = client.get(url, follow=True)
    assert resp.status_code == 200, f"Owner cannot open {url}"


@pytest.mark.parametrize("url", STAFF_READABLE)
def test_staff_surfaces_open_for_viewer_and_editor(client, django_user_model, roles, url):
    for username, group in (("mx_v2", VIEWER), ("mx_e2", EDITOR)):
        user = _mk(django_user_model, username, group=group)
        client.force_login(user)
        resp = client.get(url, follow=True)
        assert resp.status_code == 200, f"{group} cannot open {url}"
        client.logout()


@pytest.mark.parametrize("url", STAFF_READABLE)
def test_staff_surfaces_reject_anonymous(client, roles, url):
    resp = client.get(url)
    assert resp.status_code in (302, 403), f"anonymous got {resp.status_code} on {url}"


def test_viewer_cannot_reach_the_new_sale_screen(client, django_user_model, roles):
    """CLAUDE.md: «Viewer may open Сегодня/Клиенты but never Новая продажа»."""
    viewer = _mk(django_user_model, "mx_v3", group=VIEWER)
    client.force_login(viewer)
    assert client.get("/pos/sale/new/").status_code in (302, 403, 404)


def test_cost_price_never_leaks_to_a_non_owner(client, django_user_model, roles, settings):
    """Cost price is Owner-tier (CLAUDE.md). It must not appear in any page a
    Viewer or Editor can open, nor in the panel changelists they might try."""
    from decimal import Decimal

    from apps.inventory.models import Category, Product, ProductVariant

    cat = Category.objects.create(name="RoleCat")
    prod = Product.objects.create(category=cat, name="RoleProd")
    ProductVariant.objects.create(
        product=prod,
        sku="ROLE-1",
        size="M",
        color="red",
        cost_price=Decimal("1234.56"),
        sale_price=Decimal("5000"),
    )
    for username, group in (("mx_v4", VIEWER), ("mx_e4", EDITOR)):
        user = _mk(django_user_model, username, group=group)
        client.force_login(user)
        for url in ("/pos/", "/pos/today/", "/pos/clients/", "/orders/"):
            body = client.get(url, follow=True).content.decode()
            assert "1234.56" not in body and "1 234,56" not in body, (
                f"cost price leaked to {group} on {url}"
            )
        client.logout()
