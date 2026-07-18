"""Manager-facing sales terminal at /pos/. Server-rendered + HTMX partial
swaps — no API layer, no JS framework. Every screen calls the existing
apps/*/services.py functions; nothing here recomputes stock, money, or debt.
"""

from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.clients.models import Client, Interaction
from apps.clients.services import client_debt
from apps.core.currency import CURRENCY_CODES, to_base
from apps.inventory.cache import catalog_version
from apps.inventory.models import Product, ProductVariant, StockMovement
from apps.sales.models import Payment, SaleItem, SaleOrder
from apps.sales.services import (
    cancel_sale,
    confirm_sale,
    record_payment,
    return_items,
    today_summary,
)

from .decorators import pos_view
from .messaging import debt_reminder_text, receipt_text, wa_link

RECENT_CLIENTS_LIMIT = 10
PRODUCT_GRID_LIMIT = 24

_SW_SOURCE = settings.BASE_DIR / "static" / "pos" / "js" / "sw.js"


def service_worker(request):
    """Serves the service worker at /pos/sw.js rather than under /static/ —
    a service worker's default scope is its own directory, so this is the
    only URL that gives it control of all of /pos/ without needing a
    Service-Worker-Allowed header. No login required: it's static JS, not
    data. Cache-Control: no-cache so a deploy's cache-busting SHELL_VERSION
    bump (see the file itself) is picked up promptly, not held behind an
    HTTP cache on top of the browser's own SW update check."""
    content = Path(_SW_SOURCE).read_text(encoding="utf-8")
    response = HttpResponse(content, content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    response["Service-Worker-Allowed"] = "/pos/"
    return response


def require_can_sell(view):
    """Editor+ only — Viewer never reaches a sale-editing endpoint, even by
    guessing the URL (the nav link is also hidden from them in the template)."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.has_perm("sales.add_saleorder"):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _parse_decimal(raw) -> Decimal | None:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    return value if value >= 0 else None


def _own_draft_or_404(request, pk):
    return get_object_or_404(SaleOrder, pk=pk, created_by=request.user, status=SaleOrder.DRAFT)


def _sale_body_context(
    order, payment_amount=None, payment_currency=None, payment_method=None, error=None
):
    items = list(order.items.select_related("variant__product"))
    total = sum((i.line_total for i in items), Decimal("0"))
    today = timezone.localdate()
    total_conv = None
    if order.currency != settings.CURRENCY:
        total_conv = to_base(total, order.currency, today)

    currency = payment_currency or order.currency
    amount = payment_amount if payment_amount is not None else Decimal("0")
    same_currency = currency == order.currency
    balance = max(total - amount, Decimal("0")) if same_currency else total
    balance_conv = None
    if order.currency != settings.CURRENCY:
        balance_conv = to_base(balance, order.currency, today)

    paid_display = amount if same_currency else Decimal("0")
    # No status chip on an empty basket — payment_status_for(0, 0) reads as
    # "unpaid", which would wrongly badge an empty sale as a debt.
    status = SaleOrder.payment_status_for(total, paid_display) if total > 0 else None

    return {
        "order": order,
        "items": items,
        "error": error,
        "total": total,
        "total_conv": total_conv,
        "base_currency": settings.CURRENCY,
        "currencies": CURRENCY_CODES,
        "methods": Payment.METHOD_CHOICES,
        "payment_amount": payment_amount,
        "payment_currency": currency,
        "payment_method": payment_method or Payment.CASH,
        "same_currency": same_currency,
        "balance": balance,
        "balance_conv": balance_conv,
        "paid_display": paid_display,
        "status": status,
    }


# ---- Sale draft lifecycle -------------------------------------------------


@pos_view
def index(request):
    """Land on an open draft — reuse the manager's most recent one so a
    dropped connection or accidental refresh never loses the basket. ?new=1
    forces a fresh draft (an intentional 'start over'). /pos/ is also
    LOGIN_REDIRECT_URL, so a Viewer (no sales.add_saleorder) lands here too —
    route them to Сегодня instead of a dead-end 403."""
    if not request.user.has_perm("sales.add_saleorder"):
        return redirect("pos:today")
    order = None
    if request.GET.get("new") != "1":
        order = (
            SaleOrder.objects.filter(created_by=request.user, status=SaleOrder.DRAFT)
            .order_by("-created_at")
            .first()
        )
    if order is None:
        order = SaleOrder.objects.create(created_by=request.user, channel=SaleOrder.SHOP)
    return redirect("pos:sale_detail", pk=order.pk)


@pos_view
@require_can_sell
def sale_detail(request, pk):
    order = get_object_or_404(SaleOrder, pk=pk, created_by=request.user)
    if order.status != SaleOrder.DRAFT:
        return redirect("pos:sale_result", pk=order.pk)
    return render(request, "pos/sale_detail.html", {**_sale_body_context(order), "active": "sale"})


# ---- Client section (HTMX partials) ---------------------------------------


@pos_view
@require_can_sell
def client_search(request, pk):
    order = _own_draft_or_404(request, pk)
    q = request.GET.get("q", "").strip()
    if q:
        clients = Client.objects.filter(
            Q(phone__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
        ).order_by("first_name")[:RECENT_CLIENTS_LIMIT]
    else:
        clients = Client.objects.order_by("-created_at")[:RECENT_CLIENTS_LIMIT]
    return render(
        request, "pos/partials/client_results.html", {"order": order, "clients": clients, "q": q}
    )


@pos_view
@require_can_sell
def client_set(request, pk, client_id):
    order = _own_draft_or_404(request, pk)
    order.client = get_object_or_404(Client, pk=client_id)
    order.save(update_fields=["client"])
    return render(request, "pos/partials/client_section.html", {"order": order})


@pos_view
@require_can_sell
def client_clear(request, pk):
    order = _own_draft_or_404(request, pk)
    order.client = None
    order.save(update_fields=["client"])
    return render(request, "pos/partials/client_section.html", {"order": order})


@pos_view
@require_can_sell
def client_new_form(request, pk):
    order = _own_draft_or_404(request, pk)
    return render(
        request, "pos/partials/client_section.html", {"order": order, "new_client_open": True}
    )


@pos_view
@require_can_sell
def client_create(request, pk):
    order = _own_draft_or_404(request, pk)
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
            "pos/partials/client_section.html",
            {
                "order": order,
                "new_client_open": True,
                "new_client_error": error,
                "new_client_values": {
                    "first_name": first_name,
                    "last_name": last_name,
                    "phone": phone,
                },
            },
        )

    client = Client.objects.create(
        first_name=first_name, last_name=last_name, phone=phone, source=Client.SHOP
    )
    order.client = client
    order.save(update_fields=["client"])
    return render(request, "pos/partials/client_section.html", {"order": order})


# ---- Product grid + line items (HTMX partials) -----------------------------


def _build_grid_tiles(q: str) -> list[dict]:
    """The grid's catalog data (order-independent) — exactly TWO queries and a
    constant count no matter how many products exist in the DB: the products are
    LIMIT-bounded, and their variants are fetched in a single grouped query
    instead of one-per-product. Stock comes from a correlated subquery so the
    SKU-search join can't fan out and double-count it. Returns plain dicts so the
    result caches cleanly and renders per-request against the current draft."""
    stock_subquery = Subquery(
        StockMovement.objects.filter(variant__product=OuterRef("pk"))
        .values("variant__product")
        .annotate(s=Sum("quantity"))
        .values("s"),
        output_field=IntegerField(),
    )
    products = Product.objects.filter(is_active=True).select_related("category")
    if q:
        products = products.filter(Q(name__icontains=q) | Q(variants__sku__icontains=q)).distinct()
    products = list(
        products.annotate(_stock=Coalesce(stock_subquery, 0)).order_by("name")[:PRODUCT_GRID_LIMIT]
    )

    ids = [p.pk for p in products]
    variants_by_product: dict[int, list] = {pid: [] for pid in ids}
    for pid, threshold, price, currency in ProductVariant.objects.filter(
        is_active=True, product_id__in=ids
    ).values_list("product_id", "low_stock_threshold", "sale_price", "currency"):
        variants_by_product[pid].append((threshold, price, currency))

    tiles = []
    for p in products:
        variants = variants_by_product.get(p.pk, [])
        min_threshold = min((v[0] for v in variants), default=0)
        stock = p._stock or 0
        currencies_used = {v[2] for v in variants}
        # A "from X" price only means something when every variant is priced in
        # the SAME currency — comparing raw numbers across currencies (80 USD vs
        # 2500 KGS) would pick the wrong "cheapest", the exact cross-currency
        # mixup the app must never do.
        if len(currencies_used) == 1:
            cheapest = min(variants, key=lambda v: v[1])
            price, price_currency = cheapest[1], cheapest[2]
            price_varies = len({v[1] for v in variants}) > 1
        else:
            price, price_currency, price_varies = None, None, False
        image = p.grid_image
        tiles.append(
            {
                "product_id": p.pk,
                "name": p.name,
                "image_url": image.url if image else "",
                "stock": stock,
                "low": 0 < stock <= min_threshold,
                "out": stock <= 0,
                "price": price,
                "currency": price_currency,
                "price_varies": price_varies,
            }
        )
    return tiles


@pos_view
@require_can_sell
def product_grid(request, pk):
    order = _own_draft_or_404(request, pk)
    q = request.GET.get("q", "").strip()
    # Cache the catalog data (not the HTML — the HTML embeds this draft's URLs).
    # Key includes the catalog version, so any stock/product/variant write makes
    # it instantly stale (see inventory/signals.py). 60s TTL as a backstop.
    cache_key = f"grid:v{catalog_version()}:{q.lower()}"
    tiles = cache.get(cache_key)
    if tiles is None:
        tiles = _build_grid_tiles(q)
        cache.set(cache_key, tiles, 60)
    return render(
        request, "pos/partials/product_grid.html", {"order": order, "tiles": tiles, "q": q}
    )


@pos_view
@require_can_sell
def variant_picker(request, pk, product_id):
    order = _own_draft_or_404(request, pk)
    product = get_object_or_404(Product, pk=product_id)
    variants = product.variants.filter(is_active=True).annotate(
        stock_qty=Sum("movements__quantity")
    )
    return render(
        request,
        "pos/partials/variant_picker.html",
        {"order": order, "product": product, "variants": variants},
    )


@pos_view
@require_can_sell
def item_add(request, pk):
    order = _own_draft_or_404(request, pk)
    variant = get_object_or_404(ProductVariant, pk=request.POST.get("variant_id"))
    try:
        qty = max(int(request.POST.get("quantity", "1")), 1)
    except (TypeError, ValueError):
        qty = 1

    # SaleItem has no currency of its own — unit_price is implicitly stored in
    # order.currency. An empty draft adopts the first item's currency; once
    # items exist, a mismatched variant would silently mis-price (e.g. a USD
    # price read as KGS), so it's rejected instead of guessed at.
    if not order.items.exists():
        if order.currency != variant.currency:
            order.currency = variant.currency
            order.save(update_fields=["currency"])
    elif order.currency != variant.currency:
        error = _(
            "Товар в %(vc)s нельзя добавить в продажу в %(oc)s — оформите отдельной продажей."
        ) % {"vc": variant.currency, "oc": order.currency}
        return render(
            request, "pos/partials/sale_body.html", _sale_body_context(order, error=error)
        )

    existing = order.items.filter(variant=variant).first()
    if existing:
        existing.quantity += qty
        existing.save(update_fields=["quantity"])
    else:
        SaleItem.objects.create(
            order=order, variant=variant, quantity=qty, unit_price=variant.sale_price
        )
    return render(request, "pos/partials/sale_body.html", _sale_body_context(order))


@pos_view
@require_can_sell
def item_remove(request, pk, item_id):
    order = _own_draft_or_404(request, pk)
    order.items.filter(pk=item_id).delete()
    return render(request, "pos/partials/sale_body.html", _sale_body_context(order))


@pos_view
@require_can_sell
def recalc(request, pk):
    """Live Итого/Оплачено/Остаток preview as payment fields change — pure
    display, nothing persisted until confirm."""
    order = _own_draft_or_404(request, pk)
    amount = _parse_decimal(request.POST.get("amount")) or Decimal("0")
    currency = request.POST.get("currency") or order.currency
    method = request.POST.get("method") or Payment.CASH
    return render(
        request,
        "pos/partials/sale_body.html",
        _sale_body_context(
            order, payment_amount=amount, payment_currency=currency, payment_method=method
        ),
    )


# ---- Confirm / result / cancel ---------------------------------------------


@pos_view
@require_can_sell
def sale_confirm(request, pk):
    order = get_object_or_404(SaleOrder, pk=pk, created_by=request.user)
    if order.status != SaleOrder.DRAFT:
        # Already confirmed — most likely a double-tap. Idempotent: show the
        # existing result instead of erroring or creating a second sale.
        return redirect("pos:sale_result", pk=order.pk)

    try:
        confirm_sale(order, user=request.user)
    except ValidationError as exc:
        order.refresh_from_db()
        if order.status == SaleOrder.CONFIRMED:
            # Lost a race to a concurrent confirm of the same order — still idempotent.
            return redirect("pos:sale_result", pk=order.pk)
        # Redirect back to a GET-able page rather than rendering directly: a
        # refresh after an oversell must not prompt "resubmit form?", and the
        # offline-safe fetch() submit in confirm.js needs every outcome of
        # this POST to end in a redirect it can safely follow and navigate to.
        messages.error(request, "; ".join(exc.messages))
        return redirect("pos:sale_detail", pk=order.pk)

    amount = _parse_decimal(request.POST.get("amount"))
    currency = request.POST.get("currency") or order.currency
    method = request.POST.get("method") or Payment.CASH
    if amount and amount > 0:
        record_payment(order, amount, user=request.user, method=method, currency=currency)
    return redirect("pos:sale_result", pk=order.pk)


def _can_cancel(user, order) -> bool:
    if order.status != SaleOrder.CONFIRMED:
        return False
    if user.is_superuser:
        return True
    return (
        order.created_by_id == user.id
        and order.confirmed_at is not None
        # Compare in the shop's timezone, not UTC — otherwise "same-day" breaks
        # for the several evening hours when UTC has already rolled to the next
        # date but Bishkek hasn't (or vice versa in the early morning).
        and timezone.localtime(order.confirmed_at).date() == timezone.localdate()
    )


@pos_view
def sale_result(request, pk):
    if not request.user.has_perm("sales.view_saleorder"):
        raise PermissionDenied
    order = get_object_or_404(SaleOrder, pk=pk)
    if order.status == SaleOrder.DRAFT:
        return redirect("pos:sale_detail", pk=order.pk)
    items = list(order.items.select_related("variant__product"))
    status = order.payment_status if order.status == SaleOrder.CONFIRMED else None
    return render(
        request,
        "pos/result.html",
        {
            "order": order,
            "items": items,
            "status": status,
            "can_cancel": _can_cancel(request.user, order),
            "active": "sale",
        },
    )


@pos_view
@require_can_sell
def sale_cancel(request, pk):
    order = get_object_or_404(SaleOrder, pk=pk)
    if not _can_cancel(request.user, order):
        raise PermissionDenied
    try:
        cancel_sale(order, user=request.user)
    except ValidationError:
        pass  # already cancelled — idempotent no-op
    return redirect("pos:sale_result", pk=order.pk)


@pos_view
@require_can_sell
def sale_return(request, pk):
    """Partial return/exchange. Same permission as cancel (Owner any; Editor own
    same-day). GET shows a per-line quantity form; POST applies it via
    services.return_items and comes back to the result screen."""
    order = get_object_or_404(SaleOrder, pk=pk)
    if not _can_cancel(request.user, order):
        raise PermissionDenied
    items = list(order.items.select_related("variant__product"))

    if request.method == "POST":
        returns = {}
        for item in items:
            try:
                qty = int(request.POST.get(f"return_{item.pk}", "0"))
            except (TypeError, ValueError):
                qty = 0
            if qty > 0:
                returns[item.pk] = qty
        try:
            return_items(order, returns, user=request.user)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect("pos:sale_return", pk=order.pk)
        messages.success(request, _("Возврат оформлён."))
        return redirect("pos:sale_result", pk=order.pk)

    return render(request, "pos/return.html", {"order": order, "items": items, "active": "sale"})


# ---- WhatsApp receipt + debt reminder (tap-to-send, logged) ----------------


@pos_view
def share_receipt(request, pk):
    """Open WhatsApp with a ready-to-send receipt for a confirmed sale, and log
    the touchpoint. A no-op redirect back if the sale has no client/phone."""
    order = get_object_or_404(SaleOrder, pk=pk, status=SaleOrder.CONFIRMED)
    if not order.client or not order.client.phone:
        return redirect("pos:sale_result", pk=order.pk)
    link = wa_link(order.client.phone, receipt_text(order))
    if not link:  # phone had no usable digits
        return redirect("pos:sale_result", pk=order.pk)
    Interaction.objects.create(
        client=order.client,
        kind=Interaction.MESSAGE,
        note=_("Чек отправлен"),
        created_by=request.user,
    )
    return redirect(link)


@pos_view
def debt_reminder(request, pk):
    """Open WhatsApp with a polite debt reminder for a client, and log it."""
    client = get_object_or_404(Client, pk=pk)
    debts = client_debt(client)
    link = wa_link(client.phone, debt_reminder_text(client, debts)) if debts else ""
    if not link:
        return redirect("pos:client_detail", pk=client.pk)
    Interaction.objects.create(
        client=client,
        kind=Interaction.MESSAGE,
        note=_("Напоминание о долге"),
        created_by=request.user,
    )
    return redirect(link)


# ---- Today / Clients (read-only, Viewer allowed) ---------------------------


@pos_view
def today(request):
    summary = today_summary()
    today_date = timezone.localdate()
    orders = (
        SaleOrder.objects.filter(status=SaleOrder.CONFIRMED, confirmed_at__date=today_date)
        .select_related("client")
        .order_by("-confirmed_at")
    )
    unreviewed_count = Payment.objects.filter(created_at__date=today_date, reviewed=False).count()

    revenue_base = Decimal("0")
    skipped_currencies = []
    for cur, amt in summary["revenue_by_currency"].items():
        conv = to_base(amt, cur, today_date)
        if conv is None:
            skipped_currencies.append(cur)
        else:
            revenue_base += conv

    return render(
        request,
        "pos/today.html",
        {
            "summary": summary,
            "orders": orders,
            "unreviewed_count": unreviewed_count,
            "revenue_base": revenue_base,
            "base_currency": settings.CURRENCY,
            "skipped_currencies": skipped_currencies,
            "active": "today",
        },
    )


@pos_view
def clients(request):
    q = request.GET.get("q", "").strip()
    results = Client.objects.none()
    if q:
        results = Client.objects.filter(
            Q(phone__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
        ).order_by("first_name")[:50]
    return render(request, "pos/clients.html", {"clients": results, "q": q, "active": "clients"})


@pos_view
def client_detail(request, pk):
    client = get_object_or_404(Client, pk=pk)
    debts = client_debt(client)
    orders = client.sales.filter(status=SaleOrder.CONFIRMED).order_by("-confirmed_at")[:20]
    interactions = client.interactions.order_by("-created_at")[:20]
    return render(
        request,
        "pos/client_detail.html",
        {
            "client": client,
            "debts": debts,
            "orders": orders,
            "interactions": interactions,
            "active": "clients",
        },
    )
