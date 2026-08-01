"""Заказы (production orders) — manager terminal, mirrors /pos/'s sale flow
(client picker, product grid, item lines) but targets an Order/OrderItem
instead of a SaleOrder/SaleItem, and deliberately has NO stock cap: ordering
unproduced goods is the entire point (CLAUDE.md Part 3g).
"""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.clients.models import Client
from apps.core.currency import CURRENCY_CODES
from apps.inventory.models import Product, ProductVariant
from apps.pos.decorators import pos_view
from apps.sales.models import Payment

from .models import Order, OrderItem
from .services import (
    GROUP_CHOICES,
    GROUP_VARIANT,
    cancel_order,
    hand_over,
    mark_produced,
    order_line_progress,
    order_paid_amount,
    order_progress,
    production_queue,
    queue_summary,
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


"""Order-list filters. «Активные» is the default because that's the working
set — a delivered order is history, and burying today's work under months of
it is exactly the "where did my order go?" problem this page has to answer."""
STATUS_FILTERS = {
    "open": Order.OPEN_STATUSES,
    "new": [Order.NEW],
    "in_production": [Order.IN_PRODUCTION],
    "ready": [Order.READY],
    "delivered": [Order.DELIVERED],
    "cancelled": [Order.CANCELLED],
}


@pos_view
def index(request):
    status = request.GET.get("status", "open")
    if status not in STATUS_FILTERS and status != "all":
        status = "open"

    # prefetch items+deposits: order.total and order_paid_amount both walk
    # those relations per row, so without this the page is 3 queries × N.
    base = Order.objects.select_related("client").prefetch_related("items", "deposits")
    counts = {
        key: Order.objects.filter(status__in=statuses).count()
        for key, statuses in STATUS_FILTERS.items()
    }
    counts["all"] = Order.objects.count()

    orders = base if status == "all" else base.filter(status__in=STATUS_FILTERS[status])
    orders = orders.order_by("status", "due_date", "-created_at")[:200]

    rows = []
    for order in orders:
        paid = order_paid_amount(order)
        rows.append(
            {
                "order": order,
                "paid": paid,
                "remaining": max(order.total - paid, Decimal("0")),
                "items_count": len(order.items.all()),
            }
        )
    return render(
        request,
        "orders/index.html",
        {"rows": rows, "status": status, "counts": counts, "active": "orders"},
    )


@pos_view
def queue(request):
    group = request.GET.get("group", GROUP_VARIANT)
    if group not in GROUP_CHOICES:
        group = GROUP_VARIANT
    rows = production_queue(group)
    return render(
        request,
        "orders/queue.html",
        {
            "rows": rows,
            "group": group,
            "summary": queue_summary(rows),
            "active": "orders",
        },
    )


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
        # ?new=1 puts this visit on the guided route (see _wizard_mode).
        return redirect(reverse("orders:detail", args=[order.pk]) + "?new=1")
    return render(request, "orders/new.html", {"active": "orders"})


@pos_view
@require_can_sell
def client_search(request):
    q = request.GET.get("q", "").strip()
    if q:
        clients = Client.objects.filter(
            Q(phone__icontains=q) | Q(first_name__icontains=q) | Q(descriptor__icontains=q)
        ).order_by("first_name")[:10]
    else:
        clients = Client.objects.order_by("-created_at")[:10]
    return render(request, "orders/partials/client_results.html", {"clients": clients, "q": q})


@pos_view
@require_can_sell
@require_POST
def client_create(request):
    first_name = request.POST.get("first_name", "").strip()
    descriptor = request.POST.get("descriptor", "").strip()
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
                    "descriptor": descriptor,
                    "phone": phone,
                },
                "active": "orders",
            },
        )
    client = Client.objects.create(
        first_name=first_name, descriptor=descriptor, phone=phone, source=Client.SHOP
    )
    # This POST just created the client — start their order here and go to the
    # builder, rather than redirecting to a GET that creates the order (which
    # would reopen the same cross-site-GET hole `create` was closed against).
    order = Order.objects.create(client=client, created_by=request.user)
    return redirect(reverse("orders:detail", args=[order.pk]) + "?new=1")


def _build_grid_tiles(q: str) -> list[dict]:
    """Same shape as apps.pos.views._build_grid_tiles but NO cap logic — this
    is informational stock only (CLAUDE.md Part 3g: ordering unproduced goods
    is the point)."""
    from apps.inventory.models import StockMovement

    # prefetch_related, not select_related — a product can have many images
    # now; ONE extra query for the whole grid, not N (see the same comment in
    # apps.pos.views._build_grid_tiles for why Product.grid_image reads it
    # safely as a cache).
    products = (
        Product.objects.filter(is_active=True).select_related("category").prefetch_related("images")
    )
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
        "orders/partials/product_grid.html",
        {"order": order, "tiles": tiles, "q": q},
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
        "orders/partials/variant_picker.html",
        {"order": order, "product": product, "variants": variants},
    )


def _wizard_mode(request) -> bool:
    """A brand-new order walks a guided route — товары → срок → оплата →
    сохранено — while an existing one opens as a free-form edit page. The flag
    rides in the URL (?new=1) rather than in the DB, because it's a property of
    *this visit*, not of the order. HTMX posts don't carry the page's query
    string, so fall back to HX-Current-URL, which htmx always sends."""
    if request.GET.get("new") == "1":
        return True
    return "new=1" in request.headers.get("HX-Current-URL", "")


def _is_htmx(request) -> bool:
    """HTMX swaps the order body in place; a plain POST (JS blocked, or a
    test hitting the endpoint directly) still gets the redirect. Every control
    on the builder therefore works both ways — the swap is an enhancement, not
    the mechanism."""
    return request.headers.get("HX-Request") == "true"


def _body_context(request, order, error=None) -> dict:
    """Everything the order body renders — the item lines, срок, аванс and the
    totals. One context builder for the full page and for every partial swap,
    so a value can never drift between the two."""
    paid = order_paid_amount(order)
    lines = order_line_progress(order)
    return {
        "order": order,
        # `lines` carries each item plus its stock/production picture; `items`
        # stays for the plain "is this order empty" checks in the template.
        "lines": lines,
        "items": [row["item"] for row in lines],
        "progress": order_progress(order),
        "deposits": list(order.deposits.order_by("-created_at")),
        "paid": paid,
        "remaining": max(order.total - paid, Decimal("0")),
        "currencies": CURRENCY_CODES,
        "methods": Payment.METHOD_CHOICES,
        "can_write": request.user.has_perm("orders.add_order"),
        "wizard": _wizard_mode(request),
        "error": error,
    }


def _after_mutation(request, order, error=None):
    """Swap the body for HTMX, redirect otherwise. Errors ride inside the
    partial (a swapped fragment never reaches base.html's message block, so
    messages.error would silently vanish on the HTMX path)."""
    if _is_htmx(request):
        # mark_produced can auto-advance the order to готов, and item_add can
        # switch its currency — re-read so the swapped body reflects the row
        # as it now is, not as it was when the view started.
        order.refresh_from_db()
        return render(
            request, "orders/partials/order_body.html", _body_context(request, order, error)
        )
    if error:
        messages.error(request, error)
    return redirect("orders:detail", pk=order.pk)


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
    return _after_mutation(request, order)


@pos_view
@require_can_sell
@require_POST
def item_remove(request, pk, item_id):
    order = get_object_or_404(Order, pk=pk)
    order.items.filter(pk=item_id).delete()
    return _after_mutation(request, order)


@pos_view
def detail(request, pk):
    order = get_object_or_404(Order, pk=pk)
    context = _body_context(request, order)
    context["active"] = "orders"
    return render(request, "orders/detail.html", context)


@pos_view
@require_can_sell
@require_POST
def set_due_date(request, pk):
    """Setting the срок is the last step of creating an order, so saving it
    finishes the job and returns to the list rather than leaving the manager
    parked on a page with nothing left to do. Editing an existing order's
    date behaves the same way — it's still «done with this order»."""
    order = get_object_or_404(Order, pk=pk)
    raw = request.POST.get("due_date", "").strip()
    order.due_date = raw or None
    order.note = request.POST.get("note", "").strip()
    order.save(update_fields=["due_date", "note"])
    messages.success(request, _("Заказ №%(n)s сохранён.") % {"n": order.pk})
    if _is_htmx(request):
        # Tell HTMX to navigate instead of swapping — a redirect body would
        # otherwise be pasted into #order-body as a whole page.
        response = HttpResponse(status=204)
        response["HX-Redirect"] = reverse("orders:index")
        return response
    return redirect("orders:index")


def _payment_step_context(order) -> dict:
    """Step 3's numbers. The остаток is what the client will owe — but only
    once the order is handed over: an open заказ is not a sale, so it carries
    no debt yet (apps.clients.services.client_debts_by_currency counts
    CONFIRMED sales only). hand_over relinks these deposits onto the sale it
    creates, and the remainder becomes the debt from that moment."""
    paid = order_paid_amount(order)
    return {
        "order": order,
        "paid": paid,
        "remaining": max(order.total - paid, Decimal("0")),
        "deposits": list(order.deposits.order_by("-created_at")),
        "currencies": CURRENCY_CODES,
        "methods": Payment.METHOD_CHOICES,
    }


@pos_view
@require_can_sell
def step_due(request, pk):
    """Step 2 of creating an order: the срок, in its own dialog. GET opens it;
    POST saves and hands straight on to step 3 (оплата) by swapping the next
    dialog into the same slot — so the two steps read as one continuous run."""
    order = get_object_or_404(Order, pk=pk)
    if request.method == "POST":
        raw = request.POST.get("due_date", "").strip()
        order.due_date = raw or None
        order.note = request.POST.get("note", "").strip()
        order.save(update_fields=["due_date", "note"])
        return render(request, "orders/partials/step_payment.html", _payment_step_context(order))
    return render(request, "orders/partials/step_due.html", {"order": order})


@pos_view
@require_can_sell
@require_POST
def step_payment(request, pk):
    """Step 3, and the end of creation: record whatever the client paid up
    front (optional — «в долг» is a real answer), then finish. The order is
    saved either way; this dialog only decides how much of it is prepaid."""
    order = get_object_or_404(Order, pk=pk)
    amount = _parse_decimal(request.POST.get("amount"))
    if amount and amount > 0:
        try:
            record_deposit(
                order,
                amount,
                user=request.user,
                method=request.POST.get("method") or Payment.CASH,
                currency=request.POST.get("currency") or order.currency,
            )
        except ValidationError as exc:
            context = _payment_step_context(order)
            context["error"] = "; ".join(exc.messages)
            return render(request, "orders/partials/step_payment.html", context)

    messages.success(request, _("Заказ №%(n)s сохранён.") % {"n": order.pk})
    if _is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = reverse("orders:index")
        return response
    return redirect("orders:index")


@pos_view
@require_can_sell
def deliver_confirm(request, pk):
    """The «Выдать заказ» dialog. Handover is the one irreversible step — it
    confirms a sale and deducts stock — so it gets a real summary first
    instead of a browser confirm(): what leaves the warehouse, whether the
    stock is actually there, and what money is still owed. confirm_sale
    re-checks availability and raises regardless; this just means a manager
    finds out BEFORE tapping, not via an error afterwards."""
    order = get_object_or_404(Order, pk=pk)
    lines = order_line_progress(order)
    paid = order_paid_amount(order)
    return render(
        request,
        "orders/partials/deliver_modal.html",
        {
            "order": order,
            "lines": lines,
            "paid": paid,
            "remaining": max(order.total - paid, Decimal("0")),
            # available = on_hand − reserved by OTHER orders; this order's own
            # reservation is released first by hand_over, so it never blocks
            # its own conversion (see services.hand_over).
            "short_lines": [r for r in lines if not r["coverable"]],
        },
    )


@pos_view
@require_can_sell
@require_POST
def deposit_add(request, pk):
    order = get_object_or_404(Order, pk=pk)
    amount = _parse_decimal(request.POST.get("amount"))
    currency = request.POST.get("currency") or order.currency
    method = request.POST.get("method") or Payment.CASH
    error = None
    if amount and amount > 0:
        try:
            record_deposit(order, amount, user=request.user, method=method, currency=currency)
        except ValidationError as exc:
            error = "; ".join(exc.messages)
    return _after_mutation(request, order, error)


@pos_view
@require_can_sell
@require_POST
def produce(request, pk, item_id):
    order = get_object_or_404(Order, pk=pk)
    item = get_object_or_404(OrderItem, pk=item_id, order=order)
    error = None
    try:
        qty = int(request.POST.get("quantity", "0"))
    except (TypeError, ValueError):
        qty = 0
    if qty > 0:
        try:
            mark_produced(item, qty, user=request.user)
        except ValidationError as exc:
            error = "; ".join(exc.messages)
    return _after_mutation(request, order, error)


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
