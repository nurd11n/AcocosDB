"""Заказы (production orders) — manager terminal, mirrors /pos/'s sale flow
(client picker, product grid, item lines) but targets an Order/OrderItem
instead of a SaleOrder/SaleItem, and deliberately has NO stock cap: ordering
unproduced goods is the entire point (CLAUDE.md Part 3g).
"""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.clients.models import Client
from apps.core.currency import CURRENCY_CODES
from apps.inventory.models import Product, ProductVariant
from apps.pos.decorators import pos_view
from apps.sales.models import Payment

from .models import Order, OrderItem
from .services import (
    cancel_order,
    hand_over,
    mark_produced,
    order_paid_amount,
    production_queue,
    record_deposit,
)

PRODUCT_GRID_LIMIT = 24


def require_can_sell(view):
    from functools import wraps

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.has_perm("orders.add_order"):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _parse_decimal(raw):
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    return value if value is not None and value >= 0 else None


@pos_view
def index(request):
    orders = Order.objects.select_related("client").order_by("status", "due_date", "-created_at")[
        :200
    ]
    rows = []
    for order in orders:
        rows.append(
            {
                "order": order,
                "paid": order_paid_amount(order),
                "remaining": max(order.total - order_paid_amount(order), Decimal("0")),
            }
        )
    return render(request, "orders/index.html", {"rows": rows, "active": "orders"})


@pos_view
def queue(request):
    rows = production_queue()
    return render(request, "orders/queue.html", {"rows": rows, "active": "orders"})


@pos_view
@require_can_sell
def create(request):
    """GET renders the client picker (read-only). POST with a `client` id
    starts the order and lands on its detail page (the builder). Creation is
    POST-only so it can't be triggered by a cross-site GET (an <img>/link) —
    every «pick a client» row is a CSRF-protected form, not a bare link."""
    if request.method == "POST":
        client = get_object_or_404(Client, pk=request.POST.get("client"))
        order = Order.objects.create(client=client, created_by=request.user)
        return redirect("orders:detail", pk=order.pk)
    return render(request, "orders/new.html", {"active": "orders"})


@pos_view
@require_can_sell
def client_search(request):
    q = request.GET.get("q", "").strip()
    if q:
        clients = Client.objects.filter(
            Q(phone__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
        ).order_by("first_name")[:10]
    else:
        clients = Client.objects.order_by("-created_at")[:10]
    return render(request, "orders/partials/client_results.html", {"clients": clients, "q": q})


@pos_view
@require_can_sell
@require_POST
def client_create(request):
    first_name = request.POST.get("first_name", "").strip()
    last_name = request.POST.get("last_name", "").strip()
    phone = request.POST.get("phone", "").strip()
    error = None
    if not first_name or not phone:
        error = _("Имя и телефон обязательны.")
    elif Client.objects.filter(phone=phone).exists():
        error = _("Клиент с этим телефоном уже есть.")
    if error:
        return render(
            request,
            "orders/new.html",
            {
                "new_client_error": error,
                "new_client_values": {
                    "first_name": first_name,
                    "last_name": last_name,
                    "phone": phone,
                },
                "active": "orders",
            },
        )
    client = Client.objects.create(
        first_name=first_name, last_name=last_name, phone=phone, source=Client.SHOP
    )
    # This POST just created the client — start their order here and go to the
    # builder, rather than redirecting to a GET that creates the order (which
    # would reopen the same cross-site-GET hole `create` was closed against).
    order = Order.objects.create(client=client, created_by=request.user)
    return redirect("orders:detail", pk=order.pk)


def _build_grid_tiles(q: str) -> list[dict]:
    """Same shape as apps.pos.views._build_grid_tiles but NO cap logic — this
    is informational stock only (CLAUDE.md Part 3g: ordering unproduced goods
    is the point)."""
    from apps.inventory.models import StockMovement

    products = Product.objects.filter(is_active=True).select_related("category")
    if q:
        products = products.filter(Q(name__icontains=q) | Q(variants__sku__icontains=q)).distinct()
    products = list(products.order_by("name")[:PRODUCT_GRID_LIMIT])
    ids = [p.pk for p in products]

    stock = {
        row["variant__product_id"]: row["s"] or 0
        for row in StockMovement.objects.filter(variant__product_id__in=ids)
        .values("variant__product_id")
        .annotate(s=Sum("quantity"))
    }
    variants_by_product: dict[int, list] = {pid: [] for pid in ids}
    for pid, price, currency in ProductVariant.objects.filter(
        is_active=True, product_id__in=ids
    ).values_list("product_id", "sale_price", "currency"):
        variants_by_product[pid].append((price, currency))

    tiles = []
    for p in products:
        variants = variants_by_product.get(p.pk, [])
        currencies_used = {v[1] for v in variants}
        if len(currencies_used) == 1 and variants:
            cheapest = min(variants, key=lambda v: v[0])
            price, price_currency = cheapest[0], cheapest[1]
        else:
            price, price_currency = None, None
        image = p.grid_image
        tiles.append(
            {
                "product_id": p.pk,
                "name": p.name,
                "image_url": image.url if image else "",
                "stock": stock.get(p.pk, 0),
                "price": price,
                "currency": price_currency,
            }
        )
    return tiles


@pos_view
@require_can_sell
def product_grid(request, pk):
    order = get_object_or_404(Order, pk=pk)
    q = request.GET.get("q", "").strip()
    tiles = _build_grid_tiles(q)
    return render(
        request,
        "orders/product_grid.html",
        {"order": order, "tiles": tiles, "q": q, "active": "orders"},
    )


@pos_view
@require_can_sell
def variant_picker(request, pk, product_id):
    order = get_object_or_404(Order, pk=pk)
    product = get_object_or_404(Product, pk=product_id)
    variants = product.variants.filter(is_active=True).annotate(
        stock_qty=Sum("movements__quantity")
    )
    return render(
        request,
        "orders/variant_picker.html",
        {"order": order, "product": product, "variants": variants, "active": "orders"},
    )


@pos_view
@require_can_sell
@require_POST
def item_add(request, pk):
    order = get_object_or_404(Order, pk=pk)
    variant = get_object_or_404(ProductVariant, pk=request.POST.get("variant_id"))
    try:
        qty = max(int(request.POST.get("quantity", "1")), 1)
    except (TypeError, ValueError):
        qty = 1
    if not order.items.exists() and order.currency != variant.currency:
        order.currency = variant.currency
        order.save(update_fields=["currency"])
    existing = order.items.filter(variant=variant).first()
    if existing and existing.currency == variant.currency:
        existing.quantity += qty
        existing.save(update_fields=["quantity"])
    else:
        OrderItem.objects.create(
            order=order,
            variant=variant,
            quantity=qty,
            unit_price=variant.sale_price,
            currency=variant.currency,
        )
    return redirect("orders:detail", pk=order.pk)


@pos_view
@require_can_sell
@require_POST
def item_remove(request, pk, item_id):
    order = get_object_or_404(Order, pk=pk)
    order.items.filter(pk=item_id).delete()
    return redirect("orders:detail", pk=order.pk)


@pos_view
def detail(request, pk):
    order = get_object_or_404(Order, pk=pk)
    items = list(order.items.select_related("variant__product"))
    deposits = list(order.deposits.order_by("-created_at"))
    paid = order_paid_amount(order)
    return render(
        request,
        "orders/detail.html",
        {
            "order": order,
            "items": items,
            "deposits": deposits,
            "paid": paid,
            "remaining": max(order.total - paid, Decimal("0")),
            "currencies": CURRENCY_CODES,
            "methods": Payment.METHOD_CHOICES,
            "can_write": request.user.has_perm("orders.add_order"),
            "active": "orders",
        },
    )


@pos_view
@require_can_sell
@require_POST
def set_due_date(request, pk):
    order = get_object_or_404(Order, pk=pk)
    raw = request.POST.get("due_date", "").strip()
    order.due_date = raw or None
    order.note = request.POST.get("note", "").strip()
    order.save(update_fields=["due_date", "note"])
    return redirect("orders:detail", pk=order.pk)


@pos_view
@require_can_sell
@require_POST
def deposit_add(request, pk):
    order = get_object_or_404(Order, pk=pk)
    amount = _parse_decimal(request.POST.get("amount"))
    currency = request.POST.get("currency") or order.currency
    method = request.POST.get("method") or Payment.CASH
    if amount and amount > 0:
        try:
            record_deposit(order, amount, user=request.user, method=method, currency=currency)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("orders:detail", pk=order.pk)


@pos_view
@require_can_sell
@require_POST
def produce(request, pk, item_id):
    order = get_object_or_404(Order, pk=pk)
    item = get_object_or_404(OrderItem, pk=item_id, order=order)
    try:
        qty = int(request.POST.get("quantity", "0"))
    except (TypeError, ValueError):
        qty = 0
    if qty > 0:
        try:
            mark_produced(item, qty, user=request.user)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("orders:detail", pk=order.pk)


@pos_view
@require_can_sell
@require_POST
def deliver(request, pk):
    order = get_object_or_404(Order, pk=pk)
    try:
        sale = hand_over(order, user=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("orders:detail", pk=order.pk)
    return redirect("pos:sale_result", pk=sale.pk)


@pos_view
@require_POST
def cancel(request, pk):
    if not request.user.is_superuser:
        raise PermissionDenied
    order = get_object_or_404(Order, pk=pk)
    try:
        cancel_order(order, user=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("orders:detail", pk=order.pk)
