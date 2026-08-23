"""The two-step size -> color variant picker (templates/pos/partials/
_variant_picker_body.html, apps.pos.views._group_variants_for_picker) —
replaces a flat <select> of every size/color/price/qty combination on the
single most frequent action in the app.

This is deliberately PRESENTATION only: item_add's stock cap, currency
guard, and confirm_sale's own re-check are never touched by this file, and
several tests here exist specifically to prove that — the form still posts
exactly one variant_id + one quantity, same fields the old <select> posted.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command

from apps.core.permissions import EDITOR
from apps.inventory.models import Category, Product, ProductVariant, StockMovement
from apps.inventory.services import add_movement
from apps.sales.models import SaleItem, SaleOrder
from apps.sales.services import confirm_sale

pytestmark = pytest.mark.django_db


@pytest.fixture
def editor(client, django_user_model):
    call_command("setup_roles")
    user = django_user_model.objects.create_user("vp_editor", password="x" * 12, is_staff=True)
    user.groups.add(Group.objects.get(name=EDITOR))
    client.force_login(user)
    return user


@pytest.fixture
def draft(editor):
    return SaleOrder.objects.create(created_by=editor, channel=SaleOrder.SHOP)


def _make_variant(product, sku, size, color, qty, price=Decimal("1650")):
    v = ProductVariant.objects.create(
        product=product,
        sku=sku,
        size=size,
        color=color,
        cost_price=Decimal("500"),
        sale_price=price,
    )
    if qty:
        add_movement(v, StockMovement.PRODUCTION_IN, qty)
    return v


# ---------------------------------------------------------------------------
# Step-collapsing: whichever step has nothing to choose is skipped entirely
# ---------------------------------------------------------------------------


def test_single_variant_product_skips_both_steps(client, draft):
    """No size, no color — one variant. Nothing to tap through: the hidden
    field is pre-filled server-side and the quantity step is visible
    immediately, exactly the "1 variant" case the brief calls out."""
    cat = Category.objects.create(name="C")
    prod = Product.objects.create(category=cat, name="Юбка")
    v = _make_variant(prod, "SOLO-1", size="", color="", qty=5)

    body = client.get(f"/pos/sale/{draft.pk}/products/{prod.pk}/variants/").content.decode()

    assert "data-size-chip" not in body
    assert "data-colors-for" not in body
    assert "data-color-tile" not in body
    assert f'value="{v.pk}"' in body
    assert 'id="picker-qty-section" hidden' not in body, "qty step must show immediately"
    assert '<button type="submit" class="btn btn-primary" id="picker-submit" >' in body or (
        "picker-submit" in body and "disabled" not in body.split('id="picker-submit"')[1][:20]
    )


def test_size_with_exactly_one_color_still_shows_it_as_a_tile(client, draft):
    """A manager should SEE what she's about to add — even a size with only
    one color underneath it still shows that color as its own tile, rather
    than a tap on the size silently deciding the color for her. Confirmed
    directly against real usage: a size chip must never carry a variant_id
    of its own."""
    cat = Category.objects.create(name="C2")
    prod = Product.objects.create(category=cat, name="Брюки")
    va = _make_variant(prod, "P-A", size="S", color="Red", qty=5)
    vb = _make_variant(prod, "P-B", size="M", color="Blue", qty=5)

    body = client.get(f"/pos/sale/{draft.pk}/products/{prod.pk}/variants/").content.decode()

    assert "data-colors-for" in body
    for size in ("S", "M"):
        chip = body[body.index(f'data-size="{size}"') : body.index(f'data-size="{size}"') + 100]
        assert "data-variant-id" not in chip, "a size chip never picks a variant by itself"
    for v in (va, vb):
        assert f'data-variant-id="{v.pk}"' in body, "the color tile is what carries it"


def test_blank_color_still_shows_a_tile_labelled_with_an_em_dash(client, draft):
    """The exact real-world row from the report: size "42-48" with no color
    at all, alongside sizes that DO have colors. Blank is just another
    value — still shown as a one-tile step, not auto-picked from the chip,
    for the same "see what you're adding" reason a named color gets a tile."""
    cat = Category.objects.create(name="C3")
    prod = Product.objects.create(category=cat, name="Жакет")
    v42 = _make_variant(prod, "SK-42", size="42-48", color="", qty=24, price=Decimal("1550"))
    _make_variant(prod, "SK-A", size="50-56", color="Бежевый", qty=12)
    _make_variant(prod, "SK-B", size="50-56", color="Хаки", qty=36)

    body = client.get(f"/pos/sale/{draft.pk}/products/{prod.pk}/variants/").content.decode()

    chip = body[body.index('data-size="42-48"') : body.index('data-size="42-48"') + 100]
    assert "data-variant-id" not in chip
    assert 'data-colors-for="42-48"' in body
    assert 'data-colors-for="50-56"' in body
    tile = body[
        body.index(f'data-variant-id="{v42.pk}"') - 40 : body.index(f'data-variant-id="{v42.pk}"')
        + 200
    ]
    assert '<div class="tile__name">—</div>' in tile


# ---------------------------------------------------------------------------
# The full two-step picker: many sizes, many colors
# ---------------------------------------------------------------------------


def test_full_picker_disables_a_size_with_no_stock_in_any_color(client, draft):
    cat = Category.objects.create(name="C4")
    prod = Product.objects.create(category=cat, name="Платье")
    _make_variant(prod, "D-A1", size="50-56", color="Бежевый", qty=12)
    _make_variant(
        prod, "D-A2", size="50-56", color="Хаки", qty=0
    )  # size still enabled (one is in stock)
    _make_variant(prod, "D-B1", size="58-64", color="Бежевый", qty=0)
    _make_variant(prod, "D-B2", size="58-64", color="Хаки", qty=0)  # every 58-64 color is out

    body = client.get(f"/pos/sale/{draft.pk}/products/{prod.pk}/variants/").content.decode()

    chip_5056 = body[body.index('data-size="50-56"') : body.index('data-size="50-56"') + 120]
    chip_5864 = body[body.index('data-size="58-64"') : body.index('data-size="58-64"') + 120]
    assert "disabled" not in chip_5056, "50-56 has at least one available color"
    assert "disabled" in chip_5864, "every 58-64 color is out of stock"


def test_out_of_stock_colors_are_disabled_and_hidden_behind_a_toggle(client, draft):
    """Out-of-stock tiles must be non-clickable server-side (same rule the
    product grid already follows), AND tucked behind «показать нет в
    наличии» rather than padding the main list — the exact clutter the
    report screenshotted."""
    cat = Category.objects.create(name="C5")
    prod = Product.objects.create(category=cat, name="Свитер")
    in_stock = _make_variant(prod, "SW-1", size="M", color="Черный", qty=10)
    out = _make_variant(prod, "SW-2", size="M", color="Белый", qty=0)
    _make_variant(prod, "SW-3", size="L", color="Черный", qty=10)  # forces the size step to exist

    body = client.get(f"/pos/sale/{draft.pk}/products/{prod.pk}/variants/").content.decode()

    assert "показать нет в наличии" in body
    # the out-of-stock tile is disabled...
    out_tile = body[body.index(f'data-variant-id="{out.pk}"') - 40 :]
    assert "disabled" in out_tile[:200]
    # ...and sits AFTER the <details> opening tag, the in-stock one before it.
    details_pos = body.index("<details")
    in_stock_pos = body.index(f'data-variant-id="{in_stock.pk}"')
    out_pos = body.index(f'data-variant-id="{out.pk}"')
    assert in_stock_pos < details_pos < out_pos


def test_low_stock_color_tile_gets_the_partial_badge(client, draft):
    cat = Category.objects.create(name="C6")
    prod = Product.objects.create(category=cat, name="Пальто")
    v = ProductVariant.objects.create(
        product=prod,
        sku="COAT-1",
        size="M",
        color="Серый",
        cost_price=Decimal("500"),
        sale_price=Decimal("2000"),
        low_stock_threshold=3,
    )
    add_movement(v, StockMovement.PRODUCTION_IN, 2)  # <= threshold
    # A second color under the SAME (only) size — one color per size
    # collapses straight to a size-chip auto-select (tested separately
    # above), which would make `v` never reach a tile at all. With exactly
    # one size overall, the size step itself is also skipped (tested
    # separately too), so this exercises the "one size, several colors"
    # (skip_size_step) shape specifically.
    _make_variant(prod, "COAT-1B", size="M", color="Синий", qty=10)

    body = client.get(f"/pos/sale/{draft.pk}/products/{prod.pk}/variants/").content.decode()
    assert "data-size-chip" not in body, "only one size exists — no chip row needed"
    assert "data-color-tile" in body
    tile = body[
        body.index(f'data-variant-id="{v.pk}"') - 40 : body.index(f'data-variant-id="{v.pk}"') + 400
    ]
    assert "badge--partial" in tile
    assert "осталось 2" in tile


# ---------------------------------------------------------------------------
# Presentation only — the form contract and every server-side guard are
# completely unchanged. These are the load-bearing tests for this feature.
# ---------------------------------------------------------------------------


def test_add_still_only_needs_variant_id_and_quantity(client, draft):
    """What the JS actually submits: exactly the two fields the old <select>
    posted. If item_add ever grew a new required field, this breaks loudly
    instead of silently."""
    cat = Category.objects.create(name="C7")
    prod = Product.objects.create(category=cat, name="Кофта")
    v = _make_variant(prod, "K-1", size="M", color="Синий", qty=10)

    resp = client.post(f"/pos/sale/{draft.pk}/items/add/", {"variant_id": v.pk, "quantity": 3})
    assert resp.status_code == 200
    item = SaleItem.objects.get(order=draft, variant=v)
    assert item.quantity == 3


def test_cart_time_cap_still_caps_across_lines_not_per_tile(client, draft):
    """Part 1a, unmodified: adding the same variant across two picker visits
    caps against the TOTAL already in the cart — the picker's rendered
    data-max must reflect that room, not raw stock."""
    cat = Category.objects.create(name="C8")
    prod = Product.objects.create(category=cat, name="Юбка2")
    v = _make_variant(prod, "SK2-1", size="", color="", qty=15)

    client.post(f"/pos/sale/{draft.pk}/items/add/", {"variant_id": v.pk, "quantity": 12})

    body = client.get(f"/pos/sale/{draft.pk}/products/{prod.pk}/variants/").content.decode()
    assert f'value="{v.pk}" data-max="3"' in body, "15 stock - 12 in cart = 3 room left"

    # And the server still clamps a request for more than that room.
    client.post(
        f"/pos/sale/{draft.pk}/items/add/", {"variant_id": v.pk, "quantity": 10}, follow=True
    )
    item = SaleItem.objects.get(order=draft, variant=v)
    assert item.quantity == 15, "clamped to the full 15 available, never past it"


def test_single_out_of_stock_variant_is_a_dead_end_not_a_ready_form(client, draft):
    """The one case the flat <select> handled trivially (its only <option>
    was simply `disabled`) needed its own check here: with nothing else to
    choose, the picker must NOT pre-fill a ready-to-submit hidden field for a
    variant that isn't actually orderable — same "non-clickable server-side,
    not just dimmed CSS" rule a 0-stock color tile follows."""
    cat = Category.objects.create(name="C9b")
    prod = Product.objects.create(category=cat, name="Платье3")
    v = _make_variant(prod, "D3-1", size="M", color="Красный", qty=0)

    body = client.get(f"/pos/sale/{draft.pk}/products/{prod.pk}/variants/").content.decode()
    assert f'data-variant-id="{v.pk}"' not in body
    assert 'name="variant_id"' not in body, "nothing orderable — no field to even submit"
    assert "нет в наличии" in body


def test_zero_available_tile_is_rejected_server_side_even_via_direct_post(client, draft):
    """A disabled tile is UI only — item_add must still reject it by itself,
    same rule the product grid already follows for a 0-stock tile."""
    cat = Category.objects.create(name="C9")
    prod = Product.objects.create(category=cat, name="Платье2")
    v = _make_variant(prod, "D2-1", size="M", color="Красный", qty=0)
    _make_variant(prod, "D2-2", size="M", color="Синий", qty=5)  # keeps this a real color-tile case

    body = client.get(f"/pos/sale/{draft.pk}/products/{prod.pk}/variants/").content.decode()
    tile = body[body.index(f'data-variant-id="{v.pk}"') - 40 :]
    assert "disabled" in tile[:200]

    client.post(
        f"/pos/sale/{draft.pk}/items/add/", {"variant_id": v.pk, "quantity": 1}, follow=True
    )
    assert not SaleItem.objects.filter(order=draft, variant=v).exists()


def test_confirm_sale_still_re_checks_stock_regardless_of_the_picker(client, draft):
    """The picker's own numbers are a snapshot — confirm_sale's row-locked
    re-check is the real defence and must still fire exactly as before,
    completely independent of how the line got into the cart."""
    from django.core.exceptions import ValidationError

    cat = Category.objects.create(name="C10")
    prod = Product.objects.create(category=cat, name="Костюм")
    v = _make_variant(prod, "SUIT-1", size="M", color="Черный", qty=5)

    client.post(f"/pos/sale/{draft.pk}/items/add/", {"variant_id": v.pk, "quantity": 5})
    # Stock disappears from under the already-built cart (a write-off, a
    # concurrent sale finishing first — the picker can't see this coming).
    from apps.inventory.services import add_movement as _add

    _add(v, StockMovement.WRITEOFF_OUT, 5, reason="test")

    draft.refresh_from_db()
    with pytest.raises(ValidationError):
        confirm_sale(draft)


# ---------------------------------------------------------------------------
# Both entry points (existing sale vs. brand-new sale) share the same body
# ---------------------------------------------------------------------------


def test_variant_picker_new_renders_the_same_two_step_shape(client, editor):
    """/pos/sale/new/... has no order yet — a different view, but it must
    include the exact same picker body as the existing-sale path."""
    cat = Category.objects.create(name="C11")
    prod = Product.objects.create(category=cat, name="Рубашка")
    _make_variant(prod, "SH-1", size="S", color="Белый", qty=5)
    _make_variant(prod, "SH-2", size="S", color="Черный", qty=5)

    body = client.get(f"/pos/sale/new/products/{prod.pk}/variants/").content.decode()
    assert "data-size-chip" not in body, "one size only -> size step itself is skipped"
    assert "data-color-tile" in body
    assert "/pos/sale/new/items/add/" in body
