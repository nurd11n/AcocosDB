"""URL-security regressions: state-mutating endpoints must be POST-only, so a
cross-site GET (an <img>/link a logged-in manager is tricked into loading)
cannot trigger them — GET carries no CSRF token, and a GET-write would bypass
CSRF entirely. Every mutation endpoint returns 405 on GET; the few
legitimately-mixed GET/POST views (sale_return form) render on GET and only
mutate on POST. Read-only renders (search, product grid) stay GET.
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
    item = order.items.create(variant=variant, quantity=2, unit_price=Decimal("3200"))
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


# ---- WhatsApp touchpoints write, so they need POST + write permission ------


def test_receipt_and_reminder_reject_get(client, owner, variant):
    """Both create an Interaction row. As GETs they carried no CSRF token, so
    any page that got a logged-in manager to load an <img src=".../receipt/">
    could forge touchpoint history."""
    from apps.sales.models import SaleItem
    from apps.sales.services import confirm_sale

    add_movement(variant, StockMovement.PRODUCTION_IN, 5)
    cust = Client.objects.create(first_name="WaSec", phone="+996700888333")
    sale = SaleOrder.objects.create(created_by=owner, client=cust)
    SaleItem.objects.create(order=sale, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(sale, user=owner)

    for url in [
        reverse("pos:share_receipt", args=[sale.pk]),
        reverse("pos:debt_reminder", args=[cust.pk]),
    ]:
        assert client.get(url).status_code == 405, f"GET {url} should be 405"


def test_viewer_cannot_write_interactions_via_the_whatsapp_endpoints(
    client, django_user_model, variant
):
    """A Viewer is documented read-only. These endpoints write, so a Viewer
    must be refused even with a valid CSRF-bearing POST."""
    from django.contrib.auth.models import Group

    from apps.clients.models import Interaction
    from apps.core.permissions import VIEWER
    from apps.sales.models import SaleItem
    from apps.sales.services import confirm_sale

    from apps.core.permissions import EDITOR

    call_command("setup_roles")
    owner_user = django_user_model.objects.create_superuser("wa_owner", "w@e.com", "x" * 12)
    viewer = django_user_model.objects.create_user("wa_viewer", password="x" * 12, is_staff=True)
    viewer.groups.add(Group.objects.get(name=VIEWER))
    editor = django_user_model.objects.create_user("wa_editor", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))

    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    cust = Client.objects.create(first_name="WaViewer", phone="+996700888444")
    # This Editor's OWN same-day sale — share_receipt's scope (CLAUDE.md:
    # "Editor: own same-day; Owner: any") allows this one for them below.
    sale = SaleOrder.objects.create(created_by=editor, client=cust)
    SaleItem.objects.create(order=sale, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(sale, user=editor)

    client.force_login(viewer)
    assert viewer.has_perm("clients.add_interaction") is False
    before = Interaction.objects.count()
    for url in [
        reverse("pos:share_receipt", args=[sale.pk]),
        reverse("pos:debt_reminder", args=[cust.pk]),
    ]:
        assert client.post(url).status_code == 403, f"Viewer POST {url} should be 403"
    assert Interaction.objects.count() == before  # nothing written

    # An Editor, who does hold the permission, gets through — for THEIR OWN
    # same-day sale.
    client.force_login(editor)
    assert client.post(reverse("pos:share_receipt", args=[sale.pk])).status_code == 302
    assert Interaction.objects.count() == before + 1

    # A DIFFERENT Editor (someone else's sale) is refused even though they
    # hold the same permission — own-sale scope, not just the raw permission.
    other_sale = SaleOrder.objects.create(created_by=owner_user, client=cust)
    SaleItem.objects.create(
        order=other_sale, variant=variant, quantity=1, unit_price=Decimal("3200")
    )
    confirm_sale(other_sale, user=owner_user)
    resp = client.post(reverse("pos:share_receipt", args=[other_sale.pk]))
    assert resp.status_code == 403
    assert Interaction.objects.count() == before + 1  # unchanged

    # The Owner may share a receipt for ANY sale, including someone else's.
    client.force_login(owner_user)
    assert client.post(reverse("pos:share_receipt", args=[other_sale.pk])).status_code == 302
    assert Interaction.objects.count() == before + 2


# ---- Content-Security-Policy hardening -------------------------------------


def test_csp_confines_unsafe_inline_to_style_attributes(client, owner):
    """A handful of templates need per-row style attributes (progress-bar
    widths, chart colours) that no static class can express, so style-src-attr
    keeps 'unsafe-inline'. style-src-elem must NOT: an injected <style> block
    or a remote @import — the shapes used for CSS data exfiltration — stays
    refused. script-src takes no inline anything."""
    csp = client.get(reverse("pos:today")).headers["Content-Security-Policy"]
    directives = {
        part.strip().split(" ")[0]: part.strip() for part in csp.split(";") if part.strip()
    }

    assert "'unsafe-inline'" in directives["style-src-attr"]
    assert "'unsafe-inline'" not in directives["style-src-elem"]
    assert "'unsafe-inline'" not in directives["script-src"]
    assert directives["script-src-attr"] == "script-src-attr 'none'"  # no on*= handlers
    assert directives["frame-ancestors"] == "frame-ancestors 'none'"


def test_no_template_relies_on_an_inline_style_element(client, owner):
    """style-src-elem 'self' silently unstyles any page carrying a <style>
    block, so none may. The 500 page is exempt by nature — Django's exception
    path bypasses the middleware chain, so that response carries no CSP at
    all, which is why it can stay self-contained for a total outage."""
    import pathlib

    offenders = [
        str(p)
        for p in pathlib.Path("templates").rglob("*.html")
        if "templates/admin/" not in str(p)  # vendored Jazzmin: CSP-excluded
        and str(p) != "templates/errors/500.html"
        and "<style" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"inline <style> would be blocked by CSP: {offenders}"


def test_htmx_config_disables_the_style_injection_csp_blocks(client, owner):
    """L2 (2026-08-18 audit): htmx's own default (includeIndicatorStyles=true)
    injects a <style> block into <head> at runtime via insertAdjacentHTML — a
    LIVE CSP violation (style-src-elem 'self', no 'unsafe-inline'), verified
    in a real browser console before this fix, confirmed clean after. Can't
    observe htmx's own runtime JS behavior from a Django test client (no JS
    execution), so this pins down the actual fix mechanism instead: every
    page loading htmx.min.js must carry the disabling meta tag BEFORE that
    script tag (htmx reads config at its own init time), and no page may
    reach for 'unsafe-inline' as the fix instead."""
    import pathlib
    import re

    htmx_pages = [
        p
        for p in pathlib.Path("templates").rglob("*.html")
        if "vendor/htmx/htmx.min.js" in p.read_text(encoding="utf-8")
    ]
    assert htmx_pages, "expected at least one template to load htmx"
    for p in htmx_pages:
        text = p.read_text(encoding="utf-8")
        meta_pos = text.find('name="htmx-config"')
        script_pos = text.find("vendor/htmx/htmx.min.js")
        assert meta_pos != -1, f"{p} loads htmx but has no htmx-config meta tag"
        assert meta_pos < script_pos, f"{p}: htmx-config meta tag must come BEFORE the script tag"
        meta_tag = re.search(r'<meta name="htmx-config"[^>]*>', text).group()
        assert '"includeIndicatorStyles": false' in meta_tag

    resp = client.get("/pos/clients/")
    assert "'unsafe-inline'" not in resp.headers["Content-Security-Policy"].split(
        "style-src-elem"
    )[1].split(";")[0]


# ---- X-Frame-Options: /panel/'s own iframe popup vs public clickjacking ----


def test_panel_gets_sameorigin_but_public_pos_stays_deny(client, owner):
    """DENY (settings.base) refuses ANY framing, same-origin included — which
    also broke Jazzmin's own "add related object" popup (the + next to a
    select), an iframe embedding one /panel/ page inside another. /panel/
    gets SAMEORIGIN instead: still refuses a clickjacking iframe from
    someone else's site, just allows the admin's own same-origin popup. The
    public surface DENY exists to protect (/pos/, /login/) must not relax."""
    assert client.get("/panel/inventory/category/add/").headers["X-Frame-Options"] == ("SAMEORIGIN")
    assert client.get("/pos/today/").headers["X-Frame-Options"] == "DENY"
    assert client.get("/login/").headers["X-Frame-Options"] == "DENY"


# ---- L1 (2026-08-18 audit): sale_detail dead-link fix, not an access change


def test_sale_detail_redirects_a_non_creator_to_result_for_a_confirmed_sale(
    django_user_model, variant
):
    """apps/pos/views.py:899 used to 404 a non-creator hitting a link to
    someone else's already-CONFIRMED sale, even though sale_result (the
    page it should land on) was already correctly open to them via
    sales.view_saleorder. Now redirects there instead of dead-ending."""
    from django.contrib.auth.models import Group
    from django.test import Client as DjangoClient

    from apps.core.permissions import EDITOR
    from apps.sales.models import SaleItem
    from apps.sales.services import confirm_sale

    call_command("setup_roles")
    creator = django_user_model.objects.create_user("sd_creator", password="x" * 12, is_staff=True)
    creator.groups.add(Group.objects.get(name=EDITOR))
    other = django_user_model.objects.create_user("sd_other", password="x" * 12, is_staff=True)
    other.groups.add(Group.objects.get(name=EDITOR))

    add_movement(variant, StockMovement.PRODUCTION_IN, 10)
    order = SaleOrder.objects.create(created_by=creator)
    SaleItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("3200"))
    confirm_sale(order, user=creator)

    other_client = DjangoClient()
    other_client.force_login(other)
    resp = other_client.get(reverse("pos:sale_detail", args=[order.pk]))
    assert resp.status_code == 302
    assert resp.url == reverse("pos:sale_result", args=[order.pk])
    # The redirect target itself must actually be reachable — a redirect to
    # a page that then 403s would just move the dead end one hop over.
    assert other_client.get(resp.url).status_code == 200


def test_sale_detail_still_404s_a_viewer_on_someone_elses_draft(django_user_model, variant):
    """A Viewer still cannot reach sale_detail at all — but for an EXISTING,
    unrelated reason (require_can_sell's own permission check runs first and
    403s before this view's body, let alone the L1 ownership check, ever
    executes) — Viewer can't open ANY sale via this URL, draft or not. That
    gate must stay intact; this pins it down explicitly rather than leaving
    it as an assumption. See the next test for what actually exercises the
    ownership check the L1 fix touched (a role that clears require_can_sell)."""
    from django.contrib.auth.models import Group
    from django.test import Client as DjangoClient

    from apps.core.permissions import EDITOR, VIEWER

    call_command("setup_roles")
    creator = django_user_model.objects.create_user("sd_creator2", password="x" * 12, is_staff=True)
    creator.groups.add(Group.objects.get(name=EDITOR))
    viewer = django_user_model.objects.create_user("sd_viewer", password="x" * 12, is_staff=True)
    viewer.groups.add(Group.objects.get(name=VIEWER))

    draft = SaleOrder.objects.create(created_by=creator, status=SaleOrder.DRAFT)

    viewer_client = DjangoClient()
    viewer_client.force_login(viewer)
    resp = viewer_client.get(reverse("pos:sale_detail", args=[draft.pk]))
    assert resp.status_code == 403

    # The creator themselves must still reach their own draft normally.
    creator_client = DjangoClient()
    creator_client.force_login(creator)
    assert creator_client.get(reverse("pos:sale_detail", args=[draft.pk])).status_code == 200


def test_sale_detail_still_404s_another_editor_on_someone_elses_draft(django_user_model, variant):
    """What actually exercises the L1 ownership check: a role that DOES
    clear require_can_sell (so it reaches this view's body) but is NOT the
    draft's creator. The L1 fix only changes behavior for non-draft orders
    (see test_sale_detail_redirects_a_non_creator_to_result_for_a_confirmed_sale)
    — a still-DRAFT sale must stay exactly as ownership-scoped as before."""
    from django.contrib.auth.models import Group
    from django.test import Client as DjangoClient

    from apps.core.permissions import EDITOR

    call_command("setup_roles")
    creator = django_user_model.objects.create_user("sd_creator3", password="x" * 12, is_staff=True)
    creator.groups.add(Group.objects.get(name=EDITOR))
    other = django_user_model.objects.create_user("sd_other2", password="x" * 12, is_staff=True)
    other.groups.add(Group.objects.get(name=EDITOR))

    draft = SaleOrder.objects.create(created_by=creator, status=SaleOrder.DRAFT)

    other_client = DjangoClient()
    other_client.force_login(other)
    resp = other_client.get(reverse("pos:sale_detail", args=[draft.pk]))
    assert resp.status_code == 404
